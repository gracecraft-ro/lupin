---
name: ui-pressure-test
description: >-
  Run deep, human-like end-to-end tests of a web app through its real interface.
  Use this skill for major user-facing changes, release checks, or requests to
  find bugs, confusing steps, missing features, and design friction across user
  journeys, input methods, and devices. It coordinates a director and several
  small-scope evaluator agents, tests the app through the UI and its real save,
  export, and service paths, and turns findings into prioritized tickets.
compatibility: Requires a running app and browser automation with visual evidence. Multiple agents and real touch devices are recommended.
---

# Pressure-test a web app

Test the app as a person would use it. Use the real interface first. Follow the
work into the app's services and saved data. Check both whether each task works
and whether people can understand and complete it.

Use a director and several evaluator agents for a substantial test. Keep each
evaluator's assignment small so it can inspect the screens and steps in detail.

## Set safe test conditions

1. Read the repo instructions. Find how to run the app, its test accounts, test
data, supported browsers and devices, and any existing journey or design notes.
2. Use a test or preview environment with isolated accounts and data. Do not use
production, real payments, real messages, or other irreversible actions unless
the user has authorized them. Do not send unbounded traffic. This is a user-flow
test, not a load test or a security test.
3. Start the app and confirm the target build, URL, and account. Capture the
starting state. If the app or a needed account is not available, ask only for
that missing access; test the parts that are available.
4. Check the available browser and device controls. Use real browser actions
and inspect the rendered screen. Do not infer a visual result from source code,
API responses, or a passing automated test.

## Build the coverage plan

Inventory the app's user-facing tasks and visible modes before testing. Use the
repo's docs and app navigation, then confirm the list against the running app.
Include all relevant items, such as:

- Sign-in, onboarding, create, edit, save, reopen, share, and delete flows.
- Every visible creation or editing mode, including alternate tools and
  shortcuts. For a 3D app, this may include primitives, sketch tools, direct
  manipulation, and transform tools.
- Every visible export type and its options. Download each one and check that
  the file opens or can be imported again. Check its contents, not only that a
  download started.
- Empty, normal, large, and invalid inputs; validation, loading, error,
  interruption, recovery, undo, and redo states where the app supports them.
- User roles, account permissions, or feature flags when they change what a
  person can see or do.
- Safe interruptions, such as a quick repeat action, refresh or Back during a
  save, a change of input method, a screen rotation, or a paused network.
- The input methods the app claims to support: mouse clicks, hover, wheel,
  right-click, keyboard-only use, touch, drag and drop, pen, or other modes.
  Do not claim that a method passed if the browser or device could not perform
  it.
- Supported screen sizes, orientation changes, and browsers. Include narrow
  mobile, tablet, and desktop sizes when relevant.
- Keyboard focus and visible focus, labels and instructions, zoom, and other
  access needs that affect the journey. Name assistive tools that were actually
  used; do not claim a screen-reader check from keyboard testing alone.

Create a coverage table before dispatch. Give every planned journey and
variation a status: `not started`, `passed`, `finding`, `blocked`, or `not run`.
For each item, record the device, browser, input method, account or data state,
and the expected result. Do not drop a journey, mode, or export to fit one
agent's workload; add agents or run another wave. Test each supported option.
For a large set of combinations, first test each option on its own, then test
the important combinations. List every combination not tested, with the exact
reason and a next step. Do not call the test complete while safe, available
items remain untested. Never mark an untried item as passed.

## Find the core journey

Choose the most important user goal that crosses the main parts of the app. For
example: create an item, change it, save it, reload or reopen it, then export
it. Record the normal path and its expected result. Run this path once as the
baseline before splitting it into variations.

Test end to end: perform actions through the visible interface, then confirm
the result reached the real service or saved state. Reload or reopen the item
when that is part of the user's task. Check exported files and other visible
results. Use logs, network inspection, or data inspection only to confirm what
the UI did; do not use a direct API call to stand in for a user action.

## Dispatch evaluator agents

Use three or more evaluators when the agent runner supports them. Add more for
separate journeys or major tools; do not give one evaluator a large list just
to reduce agent count. If several agents are not available, work in small
separate passes and report that parallel evaluation was not available.

Start with the baseline journey. Then fan out its meaningful variations and
related journeys. Give each evaluator exactly one journey family and no more
than five paths, including the baseline if it must repeat it. Examples of
separate assignments:

- Same creation flow with mouse, keyboard-only, and touch input.
- Same flow on narrow mobile, tablet, and desktop sizes.
- A different creation mode or tool path.
- One export family, with each available format and its reopen/import check.
- A recovery path, such as refresh, undo, interruption, and resume.

Use a separate browser context, account, or data set for each evaluator. Do not
let agents change the same project or session at the same time. If state cannot
be isolated, run those assignments in sequence. Keep the core steps the same
across a variation when possible; change one main factor at a time so failures
are easy to explain. Combine factors when an interaction between them is a
likely source of failure, such as touch plus a narrow screen.

Give each evaluator a short brief with:

- The exact journey, starting state, and assigned variations.
- The target URL, build, account, test data, and safety limits.
- The device, viewport, browser, and input method to use.
- The expected result and any known project rule.
- Where to save screenshots, downloads, and other evidence.
- A limit of one journey family and five paths.

Ask evaluators to use the real UI and report only what they observed. They must
not edit app code, fix findings, or expand their assignment without telling the
director. Ask them to return a short report with the fields below.

Each evaluator returns one row per path with:

- Status: `passed`, `finding`, `blocked`, or `not run`.
- Device, browser, viewport, input method, and starting data.
- Steps completed and the result seen.
- Findings with repeat steps, user impact, and evidence links.
- Any assigned item not tested and the exact reason.

## Exercise the app

Use browser automation to click, type, scroll, drag, use keys, and take
screenshots. Inspect the actual screen after important actions. Use realistic
pace and pauses. Try mistakes and recovery, not random clicks with no user
goal.

For each assigned path:

1. Start from the stated state. Record browser, viewport, input method, and
   test data.
2. Follow the steps in order. Try the assigned variation. Note unclear words,
   hidden controls, extra steps, unexpected movement, slow or missing feedback,
   and errors that do not help the user recover.
3. Check saved state after a refresh or reopen when relevant. For a 3D app,
   inspect the shape from useful views and confirm scale or position if the task
   depends on them.
4. For each export, choose the format in the UI, download it, and inspect or
   re-import the file. Compare the result with the on-screen work.
5. Save evidence for each finding. Include the steps, screen, and relevant
   output. Keep private data out of screenshots and tickets.
6. Reset to a known state before the next variation. Report any test data that
   could not be reset.

Use touch input on a real device when one is available. Browser touch
emulation is useful, but label it as emulation. A resized desktop window is not
a mobile-device test. Test rotation and gestures only when the tool can perform
them. Mark unavailable hardware or input modes as blocked, with the exact
reason.

Do not create real irreversible side effects. Do not use a live account to test
payment, send messages, or delete user data. Use the app's safe test path or
mark that action as blocked and explain the missing setup.

## Report findings and write tickets

The director merges agent reports, removes duplicates, and checks the evidence
before filing tickets. Keep a separate ticket for each independent problem or
request. Group findings only when they share the same cause and fix. Do not
turn a preference into a bug: state the user goal, what happened, and
why it causes friction. Include design-team requests when wording, layout,
information order, control visibility, or workflow complexity is the problem.

Use the repo's `/triage` skill and issue label map. Give every ticket a `P0` to
`P3` priority, mapped to the repo's labels when needed:

- `P0`: a core task is broken or very disruptive, or the app loses or exposes
  important user data. Escalate at once. Stop a test if continuing could cause
  more harm.
- `P1`: a common or important task is blocked or badly impaired, with no
  reasonable workaround.
- `P2`: a task works but has a clear defect or repeated friction; a workaround
  exists, or the issue affects a smaller part of the task.
- `P3`: a small or rare problem with low user impact, including polish that
  does not block the task.

Do not call a finding `P0` only because it is a bug. Explain the user impact
that supports the priority. If the repo has different labels, use its mapping
and preserve the matching level of urgency.

Each ticket must include:

- A short title and type: bug, accessibility issue, feature request, or design
  request.
- Priority and affected journey, device, browser, and input method.
- User impact and the expected result.
- Exact steps to repeat the issue and what happened instead.
- Evidence links or attachments. Say if the problem happened once or more than
  once.
- A clear recommendation. For a design request, say what users need to
  understand or do; do not prescribe a visual style without evidence.
- Acceptance checks that a designer or developer can verify.

File the tickets in the project's issue tracker when access and project rules
allow. If they do not, provide complete issue-ready ticket text and state why
the tickets were not filed. Use the repo's priority and issue-type labels; do
not invent labels or change the repo's label setup.

## Finish with an honest coverage report

Give the director's report these sections:

1. **Result** — the build and environment, test window, and high-level outcome.
2. **Coverage** — the journey and variation table, with `passed`, `finding`,
   `blocked`, or `not run` status and the evidence location.
3. **Tickets** — each ticket ID or link, title, priority, and whether it is a
   bug or design request. Include issue-ready text if it could not be filed.
4. **Not tested** — every planned item not run or blocked, the exact reason,
   its impact on confidence, and the next step needed to test it.
5. **Risks** — any P0 issue, data safety concern, or unresolved uncertainty.

Do not say that everything passed if any planned item was not tested. Do not
hide gaps behind phrases such as “all major flows.” Complete the work only
when every planned item has a result, each finding has usable evidence, and
each recommendation is filed or ready to file. Keep the coverage report with
the tickets so the team can see what the test did and did not cover.
