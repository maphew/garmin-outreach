// Dashboard "Jobs" section (docs/spec-serve-ui.md section 7 Phase B/4).
// CSP-clean by construction: no inline handlers, no eval, no HTML-string
// interpolation -- only addEventListener and textContent. Not
// content-hashed -- served with Cache-Control: no-store, which is correct
// for a file that can change on re-vendor/upgrade.
//
// Live status updates (state changes, mapshare progress, completion) come
// from the SSE connection Datastar opens via `data-init="@get('/api/events')"`
// on the #jobs section (see _jobs_section.html) -- GET /api/events patches
// #job-status directly, so this file no longer polls GET /api/jobs itself.
// Its only remaining job is the click -> POST -> immediate-feedback path
// (Datastar does not perform the job-triggering POSTs; jobs.js keeps doing
// that so the CSRF header and bounded-param body stay under this project's
// control rather than an expression in an HTML attribute).
(function () {
  "use strict";

  var JOB_HEADER = "X-Garmin-Outreach-Job";

  var configEl = document.getElementById("job-config");
  var statusEl = document.getElementById("job-status");
  if (!configEl) {
    return;
  }
  var config = JSON.parse(configEl.textContent);
  var token = config.token;

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
          // Immediate feedback; the SSE stream takes over from here for
          // every subsequent state/progress change, including completion.
          renderSnapshot(result.body);
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
})();
