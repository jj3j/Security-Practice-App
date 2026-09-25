// Behavioral tests for the production handlers; no browser or network required.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../frontend/app.js'), 'utf8');
function handler(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, name);
  // All application function declarations close at column zero.
  const end = source.indexOf('\n}', start) + 2;
  return source.slice(start, end);
}
function harness(names) {
  const elements = new Map();
  const calls = [];
  const context = { state: { saveQueue: Promise.resolve(), studySaveQueue: Promise.resolve(),
      attemptId: 'attempt-1', selectedCourseId: 'course', answers: {}, attemptFlags: {},
      questions: [], payload: {}, saveFailed: false },
    el: id => { if (!elements.has(id)) elements.set(id, { textContent: '',
      classList: { toggle() {}, add() {}, remove() {} } }); return elements.get(id); },
    api: async (path, options) => { calls.push([path, options]); return {}; },
    showToast() {}, clearTimer() {}, render() {}, renderTimer() {}, showQuestionView() {},
    window: { setInterval: () => 1 }, Date, Promise, JSON, console,
  };
  vm.createContext(context);
  for (const name of names) vm.runInContext(handler(name), context);
  return { c: context, calls, elements };
}

test('autosave is ordered and captures immutable attempt and answer data', async () => {
  const {c, calls} = harness(['queueAttemptSave']);
  const update = { answers: {q1: ['A']} };
  c.queueAttemptSave(update);
  update.answers.q1 = ['B'];
  c.state.attemptId = 'attempt-2';
  c.queueAttemptSave({ flags: {q2: true} });
  await c.state.saveQueue;
  assert.equal(JSON.parse(calls[0][1].body).attempt_id, 'attempt-1');
  assert.deepEqual(JSON.parse(calls[0][1].body).answers.q1, ['A']);
  assert.equal(JSON.parse(calls[1][1].body).attempt_id, 'attempt-2');
});

test('save failures remain visible and do not break the queue', async () => {
  const {c, elements} = harness(['queueAttemptSave']);
  let count = 0;
  c.api = async () => { if (++count === 1) throw new Error('offline'); return {}; };
  await c.queueAttemptSave({answers: {q1: ['A']}});
  assert.equal(c.state.saveFailed, true);
  assert.match(elements.get('attemptSaveStatus').textContent, /unsaved/i);
  await c.queueAttemptSave({answers: {q2: ['B']}});
  assert.equal(count, 2);
  assert.match(elements.get('attemptSaveStatus').textContent, /unsaved/i);
});

test('study locations are ordered and capture course/chapter before navigation', async () => {
  const {c, calls} = harness(['recordStudyLocation']);
  c.state.currentChapter = {chapter_id: 'chapter-1'};
  c.recordStudyLocation('section-1');
  c.state.currentChapter = {chapter_id: 'chapter-2'};
  await c.recordStudyLocation(null);
  assert.equal(JSON.parse(calls[0][1].body).chapter_id, 'chapter-1');
  assert.equal(JSON.parse(calls[1][1].body).chapter_id, 'chapter-2');
});

test('resume opens the exact saved section without overwriting it with overview', async () => {
  const {c} = harness(['resumeStudy']);
  const calls = [];
  c.state.dashboard = {resume: {chapter_id:'c1', section_id:'s2'}};
  c.openChapter = async (id, options) => { calls.push([id, options.recordLocation]);
    c.state.currentChapter = {sections: [{section_id:'s1'}, {section_id:'s2'}]}; };
  c.openLesson = index => calls.push(index);
  await c.resumeStudy();
  assert.deepEqual(calls, [['c1', false], 1]);
});

test('restore uses persisted deadline, answers and flags without starting another exam', async () => {
  const {c, calls} = harness(['restoreAttempt']);
  const deadline = '2026-01-01T00:00:00+00:00';
  c.api = async path => { calls.push(path); return {attempt: {course_id:'course', assessment_kind:'exam', deadline_at:deadline},
    questions:[{question_id:'q1'}, {question_id:'q2'}], answers:{q1:['A'],q2:[]},flags:{q1:true},exam:{}}; };
  await c.restoreAttempt('restored');
  assert.equal(c.state.deadline, Date.parse(deadline));
  assert.equal(c.state.index, 1);
  assert.equal(c.state.attemptFlags.q1, true);
  assert.deepEqual(c.state.answers.q1, ['A']);
  assert.deepEqual(calls, ['/api/attempt-state?attempt_id=restored']);
});

test('restoration rejects a different course before changing state', async () => {
  const {c} = harness(['restoreAttempt']);
  c.api = async () => ({attempt:{course_id:'other'}});
  await assert.rejects(c.restoreAttempt('other'), /course first/);
  assert.equal(c.state.attemptId, 'attempt-1');
});

test('recommendation dispatch preserves action identifiers', async () => {
  const {c} = harness(['followRecommendation']);
  const calls = [];
  c.restoreAttempt = async id => calls.push(id);
  c.startSession = async (mode, bundle) => calls.push([mode,bundle]);
  c.state.dashboard = {recommended_next_step:{course_id:'course',action_type:'resume_assessment',attempt_id:'saved'}};
  await c.followRecommendation();
  c.state.dashboard.recommended_next_step = {course_id:'course',action_type:'start_practice',bundle_id:'p-001'};
  await c.followRecommendation();
  assert.deepEqual(calls, ['saved', ['practice','p-001']]);
});

test('submission waits for pending saves before sending final answers', async () => {
  const {c, calls} = harness(['finishExam']);
  c.state.mode = 'exam'; c.state.examStatus = 'active';
  c.state.questions = [{question_id:'q1'}]; c.state.answers = {q1:['A']};
  let release;
  c.state.saveQueue = new Promise(resolve => { release = resolve; });
  c.saveSelection = () => {}; c.stopTimer = () => {};
  c.loadAttemptDetail = async () => {};
  c.api = async path => { calls.push(path); return {results:[],correct_count:1,graded_count:1,percent:100,passed:true}; };
  const finishing = c.finishExam();
  await Promise.resolve();
  assert.equal(calls.length, 0);
  release(); await finishing;
  assert.deepEqual(calls, ['/api/score']);
  assert.equal(c.state.examStatus, 'completed');
});

test('restored practice answers do not dilute the graded score metric', () => {
  const {c, elements} = harness(['renderMetrics']);
  c.state.mode = 'practice';
  c.state.questions = [{question_id:'q1'}, {question_id:'q2'}, {question_id:'q3'}, {question_id:'q4'}];
  c.state.answers = {q1:['A'], q2:['B'], q3:['A'], q4:['B']};
  c.state.practiceResults = {
    q1: {selected:['A'], restored:true},
    q2: {selected:['B'], restored:true},
    q3: {selected:['A'], restored:true},
    q4: {is_correct:true, correct_answer_text:'B', explanation:''}
  };
  c.renderMetrics();
  assert.equal(elements.get('answeredMetric').textContent, '4/4');
  assert.equal(elements.get('scoreMetric').textContent, '100%');
  c.state.practiceResults.q5 = {is_correct:null};
  c.renderMetrics();
  assert.equal(elements.get('scoreMetric').textContent, '100%');
});

test('chapter opens even when the study location save fails', async () => {
  const {c} = harness(['openChapter']);
  const calls = [];
  c.api = async path => { calls.push(path); return {chapter: {chapter_id:'c1', status:'in_progress',
    sections:[{section_id:'s1'}], resume_flashcard_index:0}}; };
  c.recordStudyLocation = async () => { calls.push('location'); throw new Error('offline'); };
  c.showToast = (message) => calls.push(`toast:${message}`);
  c.renderChapter = () => calls.push('renderChapter');
  c.showOnlyView = (view) => calls.push(`view:${view}`);
  c.loadStudyCatalog = async () => {};
  await c.openChapter('c1');
  assert.deepEqual(calls, ['/api/study/chapters/c1?course_id=course', 'location',
    'renderChapter', 'view:chapterView', 'toast:Study location was not saved: offline']);
});

test('a missing dashboard hides the stale recommendation button', () => {
  const {c} = harness(['renderDashboardInsights']);
  const hidden = [];
  const elements = new Map();
  c.el = id => { if (!elements.has(id)) elements.set(id, {textContent:'', classList:{
    toggle(){}, add(){ hidden.push(id); }, remove(){} }}); return elements.get(id); };
  c.state.dashboard = null;
  c.renderDashboardInsights();
  assert.deepEqual(hidden, ['recommendedStepButton']);
});
