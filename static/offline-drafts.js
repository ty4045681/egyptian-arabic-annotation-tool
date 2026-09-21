/* Offline working drafts and immutable request outbox. IndexedDB only. */
(function (global) {
  "use strict";
  const DB_NAME = "annotation-offline-v1";
  const DB_VERSION = 1;
  const DRAFTS = "working_drafts";
  const OUTBOX = "outbox";

  function openDb() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(DRAFTS)) db.createObjectStore(DRAFTS);
        if (!db.objectStoreNames.contains(OUTBOX)) db.createObjectStore(OUTBOX);
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function withStore(storeName, mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(storeName, mode);
        const store = tx.objectStore(storeName);
        let value;
        try {
          const request = fn(store);
          if (request && typeof request === "object" && "onsuccess" in request) {
            request.onsuccess = function () { value = request.result; };
            request.onerror = function () { reject(request.error); };
          } else {
            value = request;
          }
        } catch (error) {
          reject(error);
          return;
        }
        tx.oncomplete = function () { db.close(); resolve(value); };
        tx.onerror = function () { db.close(); reject(tx.error); };
        tx.onabort = function () { db.close(); reject(tx.error); };
      });
    });
  }

  function draftKey(username, taskId) {
    return String(username) + ":" + String(taskId);
  }

  function sameJson(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function payloadMatches(current, sent) {
    if (!current || !sent) return false;
    return Object.keys(sent).every(function (key) {
      return sameJson(current[key], sent[key]);
    });
  }

  const api = {
    draftKey: draftKey,
    saveWorkingDraft: function (draft) {
      const record = Object.assign({
        schema_version: 1,
        updated_at: new Date().toISOString(),
        status: "dirty"
      }, draft);
      return withStore(DRAFTS, "readwrite", function (store) {
        store.put(record, draftKey(record.username, record.task_id));
        return record;
      });
    },
    getWorkingDraft: function (username, taskId) {
      return withStore(DRAFTS, "readonly", function (store) {
        return store.get(draftKey(username, taskId));
      }).then(function (value) { return value || null; });
    },
    listWorkingDrafts: function (username) {
      return withStore(DRAFTS, "readonly", function (store) {
        return store.getAll();
      }).then(function (rows) {
        return (rows || []).filter(function (row) { return row && row.username === username; });
      });
    },
    deleteWorkingDraft: function (username, taskId) {
      return withStore(DRAFTS, "readwrite", function (store) {
        store.delete(draftKey(username, taskId));
      });
    },
    putOutbox: function (item) {
      if (!item || !item.operation_id) {
        return Promise.reject(new Error("outbox item requires operation_id"));
      }
      const record = {
        operation_id: item.operation_id,
        username: item.username,
        task_id: item.task_id || "",
        route: item.route,
        method: item.method,
        body: item.body,
        created_at: item.created_at || new Date().toISOString(),
        attempts: item.attempts || 0
      };
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          const tx = db.transaction(OUTBOX, "readwrite");
          const store = tx.objectStore(OUTBOX);
          const getReq = store.get(record.operation_id);
          let value = record;
          let validationError = null;
          getReq.onsuccess = function () {
            const existing = getReq.result;
            if (!existing) {
              store.add(record, record.operation_id);
              return;
            }
            const immutableFields = ["username", "task_id", "route", "method"];
            const changed = immutableFields.some(function (key) {
              return String(existing[key] || "") !== String(record[key] || "");
            }) || !sameJson(existing.body, record.body);
            if (changed) {
              validationError = new Error("operation_id is already bound to a different request");
              tx.abort();
              return;
            }
            value = existing;
          };
          tx.oncomplete = function () { db.close(); resolve(value); };
          tx.onerror = function () { db.close(); reject(validationError || tx.error); };
          tx.onabort = function () { db.close(); reject(validationError || tx.error); };
        });
      });
    },
    getOutboxItem: function (operationId) {
      return withStore(OUTBOX, "readonly", function (store) {
        return store.get(operationId);
      }).then(function (value) { return value || null; });
    },
    listOutbox: function (username) {
      return withStore(OUTBOX, "readonly", function (store) {
        return store.getAll();
      }).then(function (rows) {
        return (rows || [])
          .filter(function (row) { return row && row.username === username; })
          .sort(function (a, b) { return String(a.created_at).localeCompare(String(b.created_at)); });
      });
    },
    bumpOutboxAttempt: function (operationId) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          const tx = db.transaction(OUTBOX, "readwrite");
          const store = tx.objectStore(OUTBOX);
          const getReq = store.get(operationId);
          getReq.onsuccess = function () {
            const row = getReq.result;
            if (row) {
              row.attempts = (row.attempts || 0) + 1;
              store.put(row, operationId);
            }
          };
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function () { db.close(); reject(tx.error); };
        });
      });
    },
    confirmOutbox: function (operationId, extras) {
      extras = extras || {};
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          const tx = db.transaction([OUTBOX, DRAFTS], "readwrite");
          const outbox = tx.objectStore(OUTBOX);
          const drafts = tx.objectStore(DRAFTS);
          const getReq = outbox.get(operationId);
          getReq.onsuccess = function () {
            const item = getReq.result;
            if (!item) return;
            outbox.delete(operationId);
            if (item.username && item.task_id) {
              const key = draftKey(item.username, item.task_id);
              const draftReq = drafts.get(key);
              draftReq.onsuccess = function () {
                const draft = draftReq.result;
                if (!draft) return;
                if (typeof extras.server_revision === "number") {
                  draft.server_revision = extras.server_revision;
                }
                if (extras.clear_dirty) {
                  const sentSegments = Array.isArray(item.body && item.body.segments)
                    ? item.body.segments : [];
                  const dirtyIds = new Set((draft.dirty_segment_ids || []).map(String));
                  sentSegments.forEach(function (sent) {
                    const current = (draft.segments || []).find(function (segment) {
                      return String(segment.id) === String(sent.id);
                    });
                    if (payloadMatches(current, sent)) dirtyIds.delete(String(sent.id));
                  });
                  draft.dirty_segment_ids = Array.from(dirtyIds);
                  if (item.body && Object.prototype.hasOwnProperty.call(item.body, "scene_review") &&
                      payloadMatches(draft.scene_review, item.body.scene_review)) {
                    draft.review_dirty = false;
                  }
                  draft.status = draft.dirty_segment_ids.length || draft.review_dirty
                    ? "dirty" : "clean";
                }
                if (extras.delete_draft) {
                  drafts.delete(key);
                  return;
                }
                draft.updated_at = new Date().toISOString();
                drafts.put(draft, key);
              };
            }
          };
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function () { db.close(); reject(tx.error); };
        });
      });
    },
    deleteTaskData: function (username, taskId) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          const tx = db.transaction([OUTBOX, DRAFTS], "readwrite");
          const outbox = tx.objectStore(OUTBOX);
          tx.objectStore(DRAFTS).delete(draftKey(username, taskId));
          const request = outbox.getAll();
          request.onsuccess = function () {
            (request.result || []).forEach(function (row) {
              if (row && row.username === username && String(row.task_id) === String(taskId)) {
                outbox.delete(row.operation_id);
              }
            });
          };
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function () { db.close(); reject(tx.error); };
          tx.onabort = function () { db.close(); reject(tx.error); };
        });
      });
    },
    purgeExpired: function (retentionDays, username) {
      const cutoff = Date.now() - Math.max(1, Number(retentionDays) || 7) * 86400000;
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          const tx = db.transaction([OUTBOX, DRAFTS], "readwrite");
          function sweep(store) {
            const req = store.getAll();
            req.onsuccess = function () {
              (req.result || []).forEach(function (row) {
                if (username && row.username && row.username !== username) return;
                const stamp = Date.parse(row.updated_at || row.created_at || 0);
                if (stamp && stamp < cutoff) {
                  const key = store.name === DRAFTS
                    ? draftKey(row.username, row.task_id)
                    : row.operation_id;
                  store.delete(key);
                }
              });
            };
          }
          sweep(tx.objectStore(OUTBOX));
          sweep(tx.objectStore(DRAFTS));
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function () { db.close(); reject(tx.error); };
        });
      });
    },
    exportText: function (draft) {
      const copy = Object.assign({}, draft || {});
      delete copy.mode;
      delete copy.round_id;
      return JSON.stringify(copy, null, 2);
    }
  };

  global.AnnotationOffline = api;
})(window);
