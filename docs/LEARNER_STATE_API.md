# Learner state API

This change extends the existing authenticated application. POST requests retain the session, same-origin, and CSRF checks. Every operation uses the session learner; course access is checked before returning or changing state. No production migration is performed by this change.

## Schema version 5

The existing `initialize_learning_database` migration accepts versions 1-4 and 5. It checks column presence before each addition, making repeated initialization safe.

| Table | Addition | Existing data |
| --- | --- | --- |
| `chapter_progress` | `last_section_id TEXT`, nullable | Existing rows retain status, completion time and last-viewed time; section defaults to NULL. |
| `attempt_questions` | `flagged INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1))` | Existing questions default to unflagged. |
| `attempt_answers` | No schema change | Autosaved selections use existing `selected_json` and `answered_at`; `is_correct` remains NULL until submission. |

No users, bookmarks, progress, attempts or results are recreated. Flags are scoped by the existing attempt/question key, independently of the learner's general review queue.

## Study location

`POST /api/study/location`

```json
{"course_id":"SEC530 - GDSA","chapter_id":"chapter-id","section_id":"section-id"}
```

`section_id` may be omitted or null for chapter overview. IDs must identify a currently installed chapter and a section inside it. Visits update the timestamp and section without resetting chapter completion. New visited chapters become `in_progress`.

Returns `{"resume": ...}`. `GET /api/dashboard?course_id=...` adds the same `resume` object, or null if no valid saved chapter exists:

```json
{"course_id":"SEC530 - GDSA","chapter_id":"chapter-id","section_id":"section-id","title":"Lesson title","chapter_title":"Chapter title","last_viewed_at":"ISO-8601 UTC timestamp"}
```

Resume selects the latest viewed valid chapter in the requested course. Old rows and removed section IDs fall back to chapter overview; removed chapters are skipped. All five Study screens remain separate.

## Recommended next step

Dashboard adds `recommended_next_step`, or null when no supported action is available. Every action contains `course_id`, `action_type`, and `reason`.

Priority is deterministic:

1. Latest active assessment: `resume_assessment`, `attempt_id`, `assessment_kind`; reason `unfinished_assessment`. This includes elapsed attempts so they can be submitted.
2. Most recently viewed in-progress chapter: `open_study`, `chapter_id`, nullable `section_id`; reason `unfinished_chapter`.
3. First unmastered flashcard in a visited chapter, in catalog order: `open_flashcards`, `chapter_id`, `flashcard_id`; reason `unmastered_flashcards`.
4. Missing average assessment score or average below the course passing threshold: `start_practice`, nullable `bundle_id`; reason `missing_performance` or `weak_performance`.
5. First remaining chapter: `open_study`, `chapter_id`, null `section_id`; reason `next_chapter`.
6. Available exam pathway: `choose_exam`; reason `next_pathway`.

Chapter timestamp ties use chapter ID; assessment ties use attempt ID. No AI, external service, or topic/domain classifications are used.

## Active assessment restoration

`GET /api/attempt-state?attempt_id=...`

Returns `attempt` (ID, course, mode, assessment kind, content version, status, start, original deadline and question count), ordered public `questions`, `answers` keyed by question ID, boolean `flags` keyed by question ID, `deadline_passed`, and `exam` configuration. Answers default to empty lists and flags to false. Only active attempts can use this endpoint. Practice restoration returns saved selections without grading; practice retains its existing immediate-answer workflow.

The response never includes correct answers, explanations or correctness. The existing submitted-results endpoint remains separate. Missing question records fail closed. Question text is read from the installed bank, as in the existing scoring system; this phase does not add immutable question-content snapshots.

`POST /api/attempt-state`

```json
{"attempt_id":"attempt-id","answers":{"question-id":["A"]},"flags":{"question-id":true}}
```

`answers` and `flags` are optional partial maps. An empty selection clears an answer; false clears a flag. Omitted questions are untouched. Returns `{"saved":true}` after the transaction commits. Only owned, active exam attempts before their deadline can be changed. Unknown questions, invalid choices, nonboolean flags and unsupported fields are rejected. Client state errors return 400, missing authentication 401, denied course access or CSRF failures 403.

Concurrent changes to different questions are preserved. For the same question, the last committed update wins. The browser serializes its writes and captures the attempt ID before enqueueing them. Save failures remain visible and Save Answer retries local answers and flags. Only acknowledged saves survive closing the browser; offline storage and conflict-resolution UI are not included.

## Submission and timer compatibility

`POST /api/score` retains its existing request and result contract. With an attempt ID, omitted questions use persisted selections; explicit selections override them, including an empty list. The attempt must belong to the learner and requested course and be an exam. Submission still grades and closes the attempt transactionally; later saves and repeat submissions are rejected.

The previous backend accepted late final submissions and relied on the frontend timer. This behavior is preserved, including accepting final answer selections after the deadline. New autosaves and flag edits are rejected after the original deadline, which is never extended on restore. Strict server-side rejection of late final answer changes would be a separate change to submission policy.

The legacy `/api/answer` route cannot reveal questions in the learner's active exam, and non-attempt `/api/score` is blocked while that course has an active exam. Existing practice feedback and submitted results remain available through their established flows.

There is no pause operation. Omit Stitch's Pause control or label an exit control clearly that the timer continues.

## Independent review fixes

A follow-up review of this change found the backend correct and found three frontend defects, all fixed in `frontend/app.js`.

1. Restored practice answers diluted the score metric. `renderMetrics` treated any result whose `is_correct` was not `null` as graded, but a restored answer is recorded as `{selected, restored: true}` with `is_correct` undefined. Ungraded restores therefore entered the denominator, so one correct answer beside two restores displayed `33%` instead of `100%`. The filter now requires `typeof result.is_correct === "boolean"`.
2. A failed study-location save blocked chapter navigation. `openChapter` awaited `recordStudyLocation(null)` before rendering, so a network failure threw and the chapter never opened. Location recording is telemetry and now runs fire-and-forget with a toast, matching `openLesson`.
3. A stale recommendation survived a dashboard reset. The null-dashboard branch of `renderDashboardInsights` returned before touching `recommendedStepButton`, leaving the previous course's recommendation label visible while the next dashboard loaded. That branch now hides the button.

`tests/test_frontend_state.js` gained one regression test per fix.

## Rendered-browser verification

Handler tests exercise the production functions in a VM. They are not rendered-browser evidence. This change was additionally verified in Chrome against the real frontend and the real `PracticeApi` WSGI application, served over HTTP on `127.0.0.1` from isolated temporary learner and question databases, synthetic study content, and two throwaway test identities. No persistent or production learner data was used.

Confirmed in the rendered browser:

- exact chapter and lesson resume after a full page reload, landing on the saved section rather than the chapter overview;
- navigation for the `resume_assessment`, `open_study`, `open_flashcards`, and `start_practice` recommendations, including the exact flashcard and section identifiers;
- answer and flag restoration after reload and from a second independent browser session for the same learner;
- save-failure feedback and the Save Answer retry path persisting the previously failed local answer;
- submission waiting for an in-flight save before `POST /api/score` is sent, then completing with the persisted selections;
- two concurrent sessions saving different questions, with both writes preserved;
- deadline behavior after aging `deadline_at`: autosave rejected with `400`, the timer still counting the original deadline, and the late final submission still accepted;
- no pre-submission grading disclosure in the `attempt-state` response, and grading plus explanations available only after submission;
- post-submission locks on save, state retrieval, and repeat scoring, with `attempt-detail` returning results;
- ownership isolation: a second identity received `400` for another learner's attempt on every attempt route and saw none of that learner's dashboard data;
- all five Study screens rendering and functioning, including grounded search returning indexed evidence;
- each of the three review fixes above, observed in the rendered UI.

The `choose_exam` recommendation was verified through its handler test only; reaching it in the browser requires a passing assessment average.

The Stitch project reference is authentication-gated and could not be retrieved, so this verification used the written contract in this document rather than the Stitch screens. No rendered or mobile visual-fidelity comparison against Stitch was performed.

## Local verification

- `python -m unittest discover -s tests -v`: existing regressions plus learner-state integration tests, including a populated version-4 database migrated twice. 23 tests.
- `node --test tests/test_frontend_state.js`: production-handler behavior tests for ordered saves, errors, exact Study resume, restoration, recommendations, save/submission ordering, and the three review fixes. 11 tests.
- `python scripts/validate_repo.py`
- `python -m compileall -q backend scripts tests`
- `node --check frontend/app.js`
- `git diff --check`

The legacy SQL fixture comes from commit `89ceb54c83b25bf242bf650d0d0aa02549ca8d20`. Tests use temporary databases and real local session/CSRF validation; no external identity provider is contacted.

These automated checks establish handler and repository behavior only. The rendered-browser pass above establishes real UI behavior against the same application. Neither establishes mobile or Stitch visual fidelity, live EC2 state, CI results, or production migration success. Production still runs learner schema version `4`; no production migration has been performed.
