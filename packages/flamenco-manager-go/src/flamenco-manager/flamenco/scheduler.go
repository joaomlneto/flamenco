package flamenco

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	log "github.com/Sirupsen/logrus"
	auth "github.com/abbot/go-http-auth"

	mgo "gopkg.in/mgo.v2"
	"gopkg.in/mgo.v2/bson"
)

/* Timestamp of the last time we kicked the task downloader because there weren't any
 * tasks left for workers. */
var last_upstream_check time.Time

type TaskScheduler struct {
	config   *Conf
	upstream *UpstreamConnection
	session  *mgo.Session
}

func CreateTaskScheduler(config *Conf, upstream *UpstreamConnection, session *mgo.Session) *TaskScheduler {
	return &TaskScheduler{
		config,
		upstream,
		session,
	}
}

func (ts *TaskScheduler) ScheduleTask(w http.ResponseWriter, r *auth.AuthenticatedRequest) {
	log.Infof("%s Worker %s asking for a task", r.RemoteAddr, r.Username)

	mongo_sess := ts.session.Copy()
	defer mongo_sess.Close()
	db := mongo_sess.DB("")

	// Fetch the worker's info
	projection := bson.M{"platform": 1, "supported_job_types": 1, "address": 1, "nickname": 1}
	worker, err := FindWorker(r.Username, projection, db)
	if err != nil {
		log.Warningf("%s ScheduleTask: Unable to find worker: %s", r.RemoteAddr, err)
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprintf(w, "Unable to find worker: %s", err)
		return
	}
	WorkerSeen(worker, r.RemoteAddr, db)

	var task *Task
	var was_changed bool
	for attempt := 0; attempt < 1000; attempt++ {
		// Fetch the first available task of a supported job type.
		task = ts.fetchTaskFromQueueOrManager(w, r, db, worker)
		if task == nil {
			// A response has already been written to 'w'.
			return
		}

		was_changed = ts.upstream.RefetchTask(task)
		if !was_changed {
			break
		}

		log.Debugf("Task %s was changed, reexamining queue.", task.Id.Hex())
	}
	if was_changed {
		log.Errorf("Infinite loop detected, tried 1000 tasks and they all changed...")
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	// Update the task status to "active", pushing it as a task update to the manager too.
	task.Status = "active"
	tupdate := TaskUpdate{TaskId: task.Id, TaskStatus: task.Status}
	if err := QueueTaskUpdateWithExtra(&tupdate, db, bson.M{"worker_id": worker.Id}); err != nil {
		log.Errorf("Unable to queue task update while assigning task %s to worker %s: %s",
			task.Id.Hex(), worker.Identifier(), err)
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	// Perform variable replacement on the task.
	ReplaceVariables(ts.config, task, worker)

	// Set it to this worker.
	w.Header().Set("Content-Type", "application/json")
	encoder := json.NewEncoder(w)
	encoder.Encode(task)

	log.Infof("%s assigned task %s to worker %s %s",
		r.RemoteAddr, task.Id.Hex(), r.Username, worker.Identifier())

	// Push a task log line stating we've assigned this task to the given worker.
	// This is done here, instead of by the worker, so that it's logged even if the worker fails.
	msg := fmt.Sprintf("Manager assigned task to worker %s", worker.Identifier())
	LogTaskActivity(worker, task.Id, msg, time.Now().Format(IsoFormat)+": "+msg, db)
}

/**
 * Fetches a task from either the queue, or if it is empty, from the manager.
 */
func (ts *TaskScheduler) fetchTaskFromQueueOrManager(
	w http.ResponseWriter, r *auth.AuthenticatedRequest,
	db *mgo.Database, worker *Worker) *Task {

	if len(worker.SupportedJobTypes) == 0 {
		log.Warningf("%s: worker %s has no supported job types.",
			r.RemoteAddr, worker.Id.Hex())
		w.WriteHeader(http.StatusNotAcceptable)
		fmt.Fprintln(w, "You do not support any job types.")
		return nil
	}

	result := AggregationPipelineResult{}
	tasks_coll := db.C("flamenco_tasks")

	var err error
	for attempt := 0; attempt < 2; attempt++ {
		pipe := tasks_coll.Pipe([]M{
			// 1: Select only tasks that have a runnable status & acceptable job type.
			M{"$match": M{
				"status": M{"$in": []string{"queued", "claimed-by-manager"}},
				// "job_type": M{"$in": []string{"sleeping", "testing"}},
			}},
			// 2: Unwind the parents array, so that we can do a lookup in the next stage.
			M{"$unwind": M{
				"path": "$parents",
				"preserveNullAndEmptyArrays": true,
			}},
			// 3: Look up the parent document for each unwound task.
			// This produces 1-length "parent_doc" arrays.
			M{"$lookup": M{
				"from":         "flamenco_tasks",
				"localField":   "parents",
				"foreignField": "_id",
				"as":           "parent_doc",
			}},
			// 4: Unwind again, to turn the 1-length "parent_doc" arrays into a subdocument.
			M{"$unwind": M{
				"path": "$parent_doc",
				"preserveNullAndEmptyArrays": true,
			}},
			// 5: Group by task ID to undo the unwind, and create an array parent_statuses
			// with booleans indicating whether the parent status is "completed".
			M{"$group": M{
				"_id": "$_id",
				"parent_statuses": M{"$push": M{
					"$eq": []interface{}{
						"completed",
						M{"$ifNull": []string{"$parent_doc.status", "completed"}}}}},
				// This allows us to keep all dynamic properties of the original task document:
				"task": M{"$first": "$$ROOT"},
			}},
			// 6: Turn the list of "parent_statuses" booleans into a single boolean
			M{"$project": M{
				"_id":               0,
				"parents_completed": M{"$allElementsTrue": []string{"$parent_statuses"}},
				"task":              1,
			}},
			// 7: Select only those tasks for which the parents have completed.
			M{"$match": M{
				"parents_completed": true,
			}},
			// 8: just keep the task info, the "parents_runnable" is no longer needed.
			M{"$project": M{"task": 1}},
			// 9: Sort by priority, with highest prio first. If prio is equal, use newest task.
			M{"$sort": bson.D{
				{"task.priority", -1},
				{"task._id", 1},
			}},
			// 10: Only return one task.
			M{"$limit": 1},
		})

		err = pipe.One(&result)
		if err == mgo.ErrNotFound {
			log.Infof("No tasks for worker %s found on attempt %d.", worker.Identifier(), attempt)
			dtrt := ts.config.DownloadTaskRecheckThrottle
			if attempt == 0 && dtrt >= 0 && time.Now().Sub(last_upstream_check) > dtrt {
				// On first attempt: try fetching new tasks from upstream, then re-query the DB.
				log.Infof("%s No more tasks available for %s, checking upstream",
					r.RemoteAddr, r.Username)
				last_upstream_check = time.Now()
				ts.upstream.KickDownloader(true)
				continue
			}

			log.Infof("%s Really no more tasks available for %s", r.RemoteAddr, r.Username)
			w.WriteHeader(204)
			return nil
		} else if err != nil {
			log.Errorf("%s Error fetching task for %s: %s", r.RemoteAddr, r.Username, err)
			w.WriteHeader(500)
			return nil
		}

		break
	}

	return result.Task
}
