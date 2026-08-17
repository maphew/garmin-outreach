// Dashboard "Jobs" section (docs/spec-serve-ui.md section 7 Phase B).
// CSP-clean by construction: no inline handlers, no eval, no HTML-string
// interpolation -- only addEventListener and textContent. Not
// content-hashed -- served with Cache-Control: no-store, which is correct
// for a file that can change on re-vendor/upgrade.
//
// SSE (phase 4) is intentionally not used here: after a POST accepted or
// conflicted, this polls GET /api/jobs every 2s while state is "running"
// and reloads the page once it finishes, to pick up refreshed freshness /
// layer counts. Deliberately dumb by design (see spec phase 4 note).
(function () {
  "use strict";

  var JOB_HEADER = "X-Garmin-Outreach-Job";
  var POLL_INTERVAL_MS = 2000;

  var configEl = document.getElementById("job-config");
  var statusEl = document.getElementById("job-status");
  if (!configEl) {
    return;
  }
  var config = JSON.parse(configEl.textContent);
  var token = config.token;

  var pollHandle = null;

  function renderSnapshot(snapshot) {
    if (!statusEl) {
      return;
    }
    if (!snapshot) {
      statusEl.textContent = "No job has run yet.";
      return;
    }
    var text = snapshot.kind + ": " + snapshot.state + " (started " + snapshot.started_utc + ")";
    if (snapshot.progress) {
      text += ", windows done " + snapshot.progress.windows_done;
    }
    if (snapshot.detail) {
      text += " -- " + snapshot.detail;
    }
    statusEl.textContent = text;
  }

  // Error bodies (400/403/415) are `{"error": "..."}`, not a job snapshot --
  // rendering them through renderSnapshot() prints the useless literal
  // "undefined: undefined". A stale token (server restarted since the page
  // loaded) or a browser that doesn't send Sec-Fetch-Site both surface here,
  // so the actual error text needs to reach the page.
  function renderError(status, body) {
    if (!statusEl) {
      return;
    }
    var text = body && body.error ? body.error : "request failed";
    statusEl.textContent = "error (" + status + "): " + text;
  }

  function stopPolling() {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  function pollUntilFinished() {
    stopPolling();
    pollHandle = setInterval(function () {
      fetch("/api/jobs", { credentials: "same-origin" })
        .then(function (response) {
          return response.json();
        })
        .then(function (body) {
          var current = body.current;
          if (current) {
            renderSnapshot(current);
          }
          if (!current || current.state !== "running") {
            stopPolling();
            window.location.reload();
          }
        })
        .catch(function () {
          // A transient poll failure should not wedge the UI; the next
          // tick tries again, and a page reload always recovers.
        });
    }, POLL_INTERVAL_MS);
  }

  // fetch() cannot set Sec-Fetch-Site itself (the user agent manages that
  // header), but the custom job header below is ours to add.
  function postJob(kind) {
    var headers = { "Content-Type": "application/json" };
    headers[JOB_HEADER] = token;
    fetch("/api/jobs/" + kind, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: "{}",
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { status: response.status, body: body };
        });
      })
      .then(function (result) {
        if (result.status === 202 || result.status === 409) {
          renderSnapshot(result.body);
          pollUntilFinished();
        } else {
          renderError(result.status, result.body);
        }
      })
      .catch(function () {
        // Network failure: leave the last-rendered status in place.
      });
  }

  document.querySelectorAll("[data-job-kind]").forEach(function (button) {
    button.addEventListener("click", function () {
      postJob(button.dataset.jobKind);
    });
  });

  // A page loaded while a job is already running (e.g. someone else started
  // it, or this is a reload mid-job) never sees a click -- start polling
  // immediately so its status still shows up without one.
  if (config.running) {
    pollUntilFinished();
  }
})();
