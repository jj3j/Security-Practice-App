"use strict";

const THEME_STORAGE_KEY = "security-study-theme";
const LIGHT_THEME = "light";
const DARK_THEME = "dark";
const ADMIN_PAGE_SIZE = 20;

const state = {
  payload: null,
  questions: [],
  index: 0,
  mode: "practice",
  answers: {},
  practiceResults: {},
  timerId: null,
  deadline: null,
  examStatus: "idle",
  session: null,
  courses: [],
  selectedCourseId: null,
  selectedCourse: null,
  selectedPracticeBundleId: null,
  selectedExamBundleId: null,
  bundleSelectionMode: null,
  sessionStartState: "idle",
  sessionStartError: "",
  startingBundleId: null,
  attemptId: null,
  studyCatalog: null,
  currentChapter: null,
  lessonIndex: 0,
  flashcardIndex: 0,
  flashcardRevealed: false,
  pendingIdentities: [],
  pendingIdentityOffset: 0,
  pendingIdentityTotal: 0,
  approvedIdentities: [],
  approvedIdentityOffset: 0,
  approvedIdentityTotal: 0,
  adminAuditEvents: [],
  adminAuditOffset: 0,
  adminAuditTotal: 0,
  adminSelectedIdentity: null,
  adminSelectedContentCourseId: null,
  dashboard: null,
  learnerItems: [],
  currentResults: null,
  resultFilter: "all",
  lastAssessment: null,
  searchResults: []
};

const el = (id) => document.getElementById(id);
const showEl = (id) => el(id).classList.remove("hidden");
const hideEl = (id) => el(id).classList.add("hidden");

function readThemePreference() {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === DARK_THEME
      ? DARK_THEME
      : DARK_THEME;
  } catch (error) {
    console.warn("Theme preference could not be read; using Modern Midnight mode.", error);
    return DARK_THEME;
  }
}

function applyTheme(theme, { persist = false } = {}) {
  const selectedTheme = theme === DARK_THEME ? DARK_THEME : LIGHT_THEME;
  document.documentElement.dataset.theme = selectedTheme;
  el("lightThemeButton")?.setAttribute("aria-pressed", String(selectedTheme === LIGHT_THEME));
  el("darkThemeButton")?.setAttribute("aria-pressed", String(selectedTheme === DARK_THEME));
  if (!persist) return;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, selectedTheme);
  } catch (error) {
    console.warn("Theme preference could not be saved for the next visit.", error);
    showToast("Theme changed, but this browser could not save the preference.", "neutral");
  }
}

applyTheme(readThemePreference());

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = cookieValue("__Host-gdsa_csrf");
    if (!csrfToken) throw new Error("Your session is missing request validation data. Sign in again.");
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(path, {
    ...options,
    headers
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function cookieValue(name) {
  const prefix = `${name}=`;
  for (const item of document.cookie.split(";")) {
    const value = item.trim();
    if (value.startsWith(prefix)) return value.slice(prefix.length);
  }
  return "";
}

/* ============================================================
   TOAST SYSTEM
   ============================================================ */
function showToast(message, type = "neutral") {
  const container = el("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.setAttribute("role", type === "bad" ? "alert" : "status");
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    toast.style.transition = "opacity 300ms, transform 300ms";
    setTimeout(() => toast.remove(), 350);
  }, 3800);
}

/* ============================================================
   VIEW MANAGEMENT
   ============================================================ */
function showDashboard() {
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  renderDashboard();
  showOnlyView("dashboardView");
  if (state.selectedCourseId) {
    loadDashboard().catch((error) => showToast(error.message, "bad"));
  }
}

function showExamPathway() {
  if (!state.selectedCourse?.practice_available && !state.selectedCourse?.exam_available) {
    showToast("Practice and Exam are not available for this study-only course.", "neutral");
    return;
  }
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  renderExamPathway();
  showOnlyView("examPathwayView");
}

function showPathwayChoice() {
  if (!state.selectedCourseId) {
    showCourseSelection();
    return;
  }
  renderDashboard();
  showOnlyView("pathwayChoiceView");
}

function showBundleSelection(mode) {
  const bundles = bundlesForMode(mode);
  if (!bundles.length) {
    startSession(mode, null).catch((error) => showToast(error.message, "bad"));
    return;
  }
  state.bundleSelectionMode = mode;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  state.selectedPracticeBundleId = mode === "practice" ? null : state.selectedPracticeBundleId;
  state.selectedExamBundleId = mode === "exam" ? null : state.selectedExamBundleId;
  renderBundleSelection();
  showOnlyView("bundleSelectionView");
}

function showQuestionView() {
  closeQuestionMap();
  showOnlyView("questionView");
}

function openQuestionMap() {
  el("questionSessionPanel").classList.add("is-open");
  el("questionMapBackdrop").classList.remove("hidden");
  el("questionMapToggle").setAttribute("aria-expanded", "true");
  el("questionMapClose").focus();
}

function closeQuestionMap({ restoreFocus = false } = {}) {
  const panel = el("questionSessionPanel");
  if (!panel) return;
  panel.classList.remove("is-open");
  el("questionMapBackdrop").classList.add("hidden");
  el("questionMapToggle").setAttribute("aria-expanded", "false");
  if (restoreFocus) el("questionMapToggle").focus();
}

function showOnlyView(viewId) {
  for (const view of document.querySelectorAll("#applicationMain > .view")) {
    view.classList.toggle("hidden", view.id !== viewId);
  }
  for (const button of document.querySelectorAll("[data-nav-view]")) {
    const target = button.dataset.navView;
    const active = target === viewId
      || (target === "dashboardView" && viewId === "pathwayChoiceView")
      || (target === "studyView" && ["chapterView", "lessonView", "flashcardView", "groundedView"].includes(viewId))
      || (target === "examPathwayView" && ["bundleSelectionView", "questionView", "resultsView"].includes(viewId));
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  }
  window.scrollTo(0, 0);
  window.requestAnimationFrame(() => {
    const heading = el(viewId)?.querySelector('[tabindex="-1"]');
    heading?.focus({ preventScroll: false });
  });
}

function showCourseSelection() {
  state.selectedCourseId = null;
  state.selectedCourse = null;
  state.selectedPracticeBundleId = null;
  state.selectedExamBundleId = null;
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  state.dashboard = null;
  state.learnerItems = [];
  showOnlyView("courseSelectionView");
  renderCourses();
}

function goToDashboard() {
  stopTimer();
  state.deadline = null;
  state.examStatus = "idle";
  state.mode = "practice";
  state.attemptId = null;
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  if (state.selectedCourseId) {
    showDashboard();
  } else {
    showCourseSelection();
  }
}

function modePayload(mode) {
  if (mode === "practice") return state.payload?.practice || {};
  if (mode === "exam") return state.payload?.exam || {};
  throw new Error(`Unsupported assessment mode: ${mode}`);
}

function bundlesForMode(mode) {
  const bundles = modePayload(mode).bundles;
  return Array.isArray(bundles) ? bundles : [];
}

function setInlineStatus(elementId, message, type = "neutral") {
  const status = el(elementId);
  const isError = type === "error";
  status.textContent = message;
  status.className = `status-message ${type}${message ? "" : " hidden"}`;
  status.setAttribute("role", isError ? "alert" : "status");
  status.setAttribute("aria-live", isError ? "assertive" : "polite");
}

/* ============================================================
   RENDER DASHBOARD
   ============================================================ */
function renderDashboard() {
  if (!state.payload) return;
  const course = state.selectedCourse || {};
  const courseId = course.course_id || state.payload.project_id || "Course";
  el("selectedCourseLabel").textContent = courseId;
  el("pathwayBreadcrumbCourse").textContent = courseId;
  el("pathwayChoiceCourse").textContent = courseId;
  el("pathwayCourseTitle").textContent = course.title
    ? `Your saved progress for ${course.title}.`
    : "Track your saved study and assessment progress.";
  el("pathwayChoiceDescription").textContent = course.title
    ? `Choose your approach for ${course.title}. Build foundational knowledge in Study or test your readiness in Examination.`
    : "Choose foundational study or a readiness assessment.";

  const chapters = state.studyCatalog?.chapters || [];
  const completed = chapters.filter((chapter) => chapter.status === "completed").length;
  const unitLabels = studyUnitLabels();
  el("chapterBackButton").textContent = `Study ${unitLabels.plural.toLowerCase()}`;
  el("dashboardStudyDescription").textContent = course.study_available
    ? `Review ${unitLabels.plural.toLowerCase()}, use flashcards, and ask source-grounded questions.`
    : "Study guides are not available for this course yet. Use practice bundles and exams.";
  el("dashChapterProgress").textContent = course.study_available
    ? `${completed} of ${chapters.length} ${unitLabels.plural.toLowerCase()} complete`
    : "Not available for this course";
  el("studyPathwayCard").classList.toggle("unavailable", !course.study_available);
  el("dashboardStudyBtn").disabled = !course.study_available;
  el("dashboardStudyBtn").textContent = course.study_available ? "Enter study module" : "Study unavailable";
  const assessmentAvailable = Boolean(course.practice_available || course.exam_available);
  el("examPathwayCard").classList.toggle("unavailable", !assessmentAvailable);
  el("dashboardExamPathBtn").disabled = !assessmentAvailable;
  el("dashboardExamPathBtn").textContent = assessmentAvailable
    ? "Open examination module"
    : "Examination unavailable";
  el("sidebarExamButton").disabled = !assessmentAvailable;
  const practiceBundles = bundlesForMode("practice");
  const examBundles = bundlesForMode("exam");
  const bundleCount = Math.max(practiceBundles.length, examBundles.length);
  el("examPathwaySummary").textContent = !assessmentAvailable
    ? "Study-only course"
    : bundleCount
      ? `${bundleCount} structured ${bundleCount === 1 ? "bundle" : "bundles"} available`
      : "Practice and mock exam options available";
  renderDashboardInsights();
}

function formatScore(value) {
  return Number.isFinite(Number(value)) ? `${Math.round(Number(value))}%` : "—";
}

function renderDashboardMetric(id, value, unit = "") {
  const target = el(id);
  target.replaceChildren();
  if (!unit) {
    target.textContent = value;
    return;
  }
  const valueNode = document.createElement("span");
  valueNode.className = "metric-value";
  valueNode.textContent = value;
  const unitNode = document.createElement("span");
  unitNode.className = "metric-unit";
  unitNode.textContent = unit;
  target.append(valueNode, unitNode);
}

function attemptLabel(attempt) {
  const kind = attempt.assessment_kind === "practice" ? "Practice" : "Exam";
  const rawBundle = String(attempt.content_version || "assessment").replace(/^practice:/, "");
  const match = rawBundle.match(/bundle-(\d+)$/);
  return match ? `${kind} · Bundle ${Number(match[1])}` : `${kind} · ${rawBundle}`;
}

function renderDashboardInsights() {
  const dashboard = state.dashboard;
  if (!dashboard) {
    el("dashboardStudyMetric").textContent = "—";
    el("dashboardAssessmentMetric").textContent = "—";
    el("dashboardAverageMetric").textContent = "—";
    el("dashboardReviewMetric").textContent = "—";
    return;
  }
  const metrics = dashboard.metrics || {};
  const unitLabels = studyUnitLabels();
  if (metrics.chapter_total) {
    renderDashboardMetric(
      "dashboardStudyMetric",
      `${metrics.chapter_completed}/${metrics.chapter_total}`,
      unitLabels.plural.toLowerCase()
    );
  } else {
    renderDashboardMetric("dashboardStudyMetric", "No study guide");
  }
  renderDashboardMetric("dashboardAssessmentMetric", String(metrics.completed_assessments || 0));
  renderDashboardMetric("dashboardAverageMetric", formatScore(metrics.average_score));
  renderDashboardMetric("dashboardReviewMetric", String(metrics.review_count || 0));

  const attempts = dashboard.recent_attempts || [];
  el("dashboardAttemptStatus").textContent = attempts.length ? `${attempts.length} recent` : "No attempts yet";
  const attemptList = el("recentAttemptList");
  attemptList.replaceChildren();
  for (const attempt of attempts) {
    const row = document.createElement("article");
    row.className = "compact-list-row";
    const details = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = attemptLabel(attempt);
    const meta = document.createElement("span");
    const status = attempt.status === "submitted" ? formatScore(attempt.score_percent) : "In progress";
    meta.textContent = `${status} · ${formatIdentityTimestamp(attempt.started_at)}`;
    details.append(title, meta);
    const action = document.createElement("button");
    action.type = "button";
    action.className = "ghost";
    action.textContent = "Review";
    action.disabled = attempt.status !== "submitted";
    action.addEventListener("click", () => {
      loadAttemptDetail(attempt.attempt_id).catch((error) => showToast(error.message, "bad"));
    });
    row.append(details, action);
    attemptList.appendChild(row);
  }
  if (!attempts.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Completed practice and exam attempts will appear here.";
    attemptList.appendChild(empty);
  }

  state.learnerItems = Array.isArray(dashboard.items) ? dashboard.items : [];
  el("dashboardItemStatus").textContent = state.learnerItems.length
    ? `${state.learnerItems.length} saved`
    : "Nothing saved";
  const itemList = el("savedItemList");
  itemList.replaceChildren();
  for (const item of state.learnerItems.slice(0, 8)) {
    const row = document.createElement("article");
    row.className = "compact-list-row";
    const details = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.textContent = item.item_type === "lesson" ? "Bookmarked lesson" : "Review question";
    details.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "compact-actions";
    if (item.item_type === "lesson") {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "ghost";
      open.textContent = "Open";
      open.addEventListener("click", () => {
        openSavedLesson(item).catch((error) => showToast(error.message, "bad"));
      });
      actions.appendChild(open);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => {
      updateLearnerItem(item.item_type, item.item_id, false).catch((error) => showToast(error.message, "bad"));
    });
    actions.appendChild(remove);
    row.append(details, actions);
    itemList.appendChild(row);
  }
  if (!state.learnerItems.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Bookmark lessons or add missed questions to your review queue.";
    itemList.appendChild(empty);
  }
}

async function loadDashboard(courseId = state.selectedCourseId) {
  if (!courseId) return;
  state.dashboard = await api(`/api/dashboard?course_id=${encodeURIComponent(courseId)}`);
  renderDashboardInsights();
}

async function openSavedLesson(item) {
  await openChapter(item.chapter_id);
  const index = (state.currentChapter?.sections || []).findIndex(
    (section) => section.section_id === item.item_id
  );
  openLesson(index >= 0 ? index : 0);
}

async function updateLearnerItem(itemType, itemId, present) {
  await api("/api/learner-items", {
    method: "POST",
    body: JSON.stringify({
      course_id: state.selectedCourseId,
      item_type: itemType,
      item_id: itemId,
      present
    })
  });
  await loadDashboard();
  if (state.currentResults) {
    const result = state.currentResults.results.find((item) => item.question_id === itemId);
    if (result) result.in_review_queue = present;
    renderResults();
  }
  if (itemType === "lesson") renderLessonBookmark();
  showToast(present ? "Saved for later." : "Removed from saved items.", "ok");
}

function renderExamPathway() {
  if (!state.payload) return;
  const course = state.selectedCourse || {};
  const practice = modePayload("practice");
  const exam = modePayload("exam");
  const practiceBundles = bundlesForMode("practice");
  const examBundles = bundlesForMode("exam");
  const practiceCount = practice.target_question_count || practice.available_question_count || 0;
  const examCount = exam.target_question_count || exam.available_question_count || 0;
  const examMinutes = Math.round((exam.duration_seconds || 0) / 60);

  el("practicePathwayMeta").textContent = !course.practice_available
    ? "Not available for this course"
    : practiceBundles.length
      ? `${practiceBundles.length} ${practiceBundles.length === 1 ? "bundle" : "bundles"} · Up to ${practiceCount} questions · Unlimited time`
      : `${practice.available_question_count || practiceCount} questions · Unlimited time`;
  el("examPathwayMeta").textContent = !course.exam_available
    ? "Not available for this course"
    : examBundles.length
      ? `${examBundles.length} ${examBundles.length === 1 ? "bundle" : "bundles"} · Up to ${examCount} questions · ${examMinutes} minutes`
      : `${Math.min(examCount, exam.available_question_count || examCount)} questions · ${examMinutes} minutes`;
  el("dashboardExamTitle").textContent = exam.label || "Mock exam";
  el("dashboardPracticeBtn").textContent = practiceBundles.length ? "Choose practice bundle" : "Start practice";
  el("dashboardExamBtn").textContent = examBundles.length ? "Choose exam bundle" : "Start mock exam";
  const isStarting = state.sessionStartState === "loading";
  el("dashboardPracticeBtn").disabled = isStarting || !course.practice_available;
  el("dashboardExamBtn").disabled = isStarting || !course.exam_available;
  if (!course.practice_available) el("dashboardPracticeBtn").textContent = "Practice unavailable";
  if (!course.exam_available) el("dashboardExamBtn").textContent = "Exam unavailable";
  el("dashboardPracticeBtn").setAttribute("aria-busy", String(isStarting && state.bundleSelectionMode === "practice"));
  el("dashboardExamBtn").setAttribute("aria-busy", String(isStarting && state.bundleSelectionMode === "exam"));
  setInlineStatus("examPathwayStatus", state.sessionStartError, "error");
}

function renderBundleSelection() {
  const mode = state.bundleSelectionMode;
  if (!mode) return;
  const assessment = modePayload(mode);
  const bundles = bundlesForMode(mode);
  const isExam = mode === "exam";
  const minutes = Math.round((state.payload.exam?.duration_seconds || 0) / 60);
  const grid = el("bundleGrid");
  grid.replaceChildren();

  el("bundleBreadcrumbMode").textContent = isExam ? "Mock exam bundles" : "Practice bundles";
  el("bundleSelectionEyebrow").textContent = isExam ? "Timed assessment" : "Guided assessment";
  el("bundleSelectionTitle").textContent = isExam ? "Choose a mock exam bundle" : "Choose a practice bundle";
  el("bundleSelectionDescription").textContent = isExam
    ? `Each full bundle contains ${assessment.target_question_count} questions and allows ${minutes} minutes. The final bundle may be smaller.`
    : `Each full bundle contains ${assessment.target_question_count} questions with immediate feedback and unlimited time. The final bundle may be smaller.`;

  for (const bundle of bundles) {
    const card = document.createElement("article");
    card.className = "bundle-card";

    const header = document.createElement("div");
    header.className = "bundle-card-header";
    const label = document.createElement("span");
    label.className = "bundle-chip";
    label.textContent = bundle.label;
    header.appendChild(label);
    if (!bundle.full_size) {
      const partial = document.createElement("span");
      partial.className = "bundle-note";
      partial.textContent = "Final partial bundle";
      header.appendChild(partial);
    }

    const title = document.createElement("h2");
    title.textContent = `${bundle.question_count} questions`;
    const details = document.createElement("p");
    details.className = "bundle-details";
    details.textContent = isExam
      ? `${minutes} minutes · ${assessment.passing_score_percent}% passing score`
      : "Unlimited time · Immediate answer feedback";
    const range = document.createElement("p");
    range.className = "bundle-range";
    const firstQuestion = (bundle.bundle_number - 1) * assessment.target_question_count + 1;
    const lastQuestion = firstQuestion + bundle.question_count - 1;
    range.textContent = `Questions ${firstQuestion}–${lastQuestion}`;

    const action = document.createElement("button");
    action.type = "button";
    action.className = "primary bundle-start";
    action.dataset.bundleId = bundle.bundle_id;
    const isThisBundleStarting = state.sessionStartState === "loading" && state.startingBundleId === bundle.bundle_id;
    action.textContent = isThisBundleStarting
      ? "Starting…"
      : isExam ? "Start mock exam" : "Start practice";
    action.disabled = state.sessionStartState === "loading";
    action.classList.toggle("is-loading", isThisBundleStarting);
    action.setAttribute("aria-busy", String(isThisBundleStarting));
    action.setAttribute(
      "aria-label",
      `${action.textContent} ${bundle.label}, ${bundle.question_count} questions, ${isExam ? `${minutes} minutes` : "unlimited time"}`
    );
    action.addEventListener("click", () => {
      startSession(mode, bundle.bundle_id).catch((error) => showToast(error.message, "bad"));
    });
    card.append(header, title, details, range, action);
    grid.appendChild(card);
  }

  const statusMessage = state.sessionStartState === "loading"
    ? "Preparing your assessment…"
    : state.sessionStartError;
  setInlineStatus(
    "bundleSelectionStatus",
    statusMessage,
    state.sessionStartState === "error" ? "error" : "neutral"
  );
}

function renderCourses() {
  const grid = el("courseGrid");
  grid.replaceChildren();
  for (const course of state.courses) {
    const card = document.createElement("article");
    card.className = `course-card${course.available ? "" : " unavailable"}`;

    const code = document.createElement("p");
    code.className = "eyebrow";
    code.textContent = course.course_id;
    const title = document.createElement("h2");
    title.textContent = course.title;
    const description = document.createElement("p");
    description.textContent = course.available
      ? course.study_available && (course.practice_available || course.exam_available)
        ? "Focused study lessons, flashcards, practice questions, and a timed exam simulation."
        : course.study_available
          ? "Focused study lessons, flashcards, and source-grounded explanations."
          : "Structured practice bundles and timed mock exams."
      : "Course content and learner progress support will be added in a future milestone.";
    const action = document.createElement("button");
    action.type = "button";
    action.className = course.available ? "primary" : "ghost";
    action.textContent = course.available ? "Open course" : (course.status || "Coming soon");
    action.disabled = !course.available;
    if (course.available) {
      action.addEventListener("click", () => selectCourse(course.course_id));
    }
    card.append(code, title, description, action);
    grid.appendChild(card);
  }
}

async function selectCourse(courseId) {
  const course = state.courses.find((item) => item.course_id === courseId);
  if (!course?.available) return;
  state.selectedCourseId = courseId;
  state.selectedCourse = course;
  state.selectedPracticeBundleId = null;
  state.selectedExamBundleId = null;
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  try {
    const assessmentAvailable = Boolean(course.practice_available || course.exam_available);
    const loaders = [loadDashboard(courseId)];
    if (assessmentAvailable) {
      loaders.push(load(courseId));
    } else {
      state.payload = {
        project_id: course.course_id,
        course,
        practice: course.practice || {},
        exam: course.exam || {},
        questions: []
      };
      state.questions = [];
    }
    if (course.study_available) {
      loaders.push(loadStudyCatalog(courseId));
    } else {
      state.studyCatalog = null;
    }
    await Promise.all(loaders);
    showDashboard();
  } catch (error) {
    state.selectedCourseId = null;
    state.selectedCourse = null;
    state.selectedPracticeBundleId = null;
    state.selectedExamBundleId = null;
    state.bundleSelectionMode = null;
    state.sessionStartState = "idle";
    state.sessionStartError = "";
    state.startingBundleId = null;
    showToast(error.message, "bad");
  }
}

async function loadCourses() {
  const payload = await api("/api/courses");
  state.courses = Array.isArray(payload.courses) ? payload.courses : [];
  if (!state.courses.length) throw new Error("No courses are configured.");
  renderCourses();
}

async function loadStudyCatalog(courseId = state.selectedCourseId) {
  state.studyCatalog = await api(`/api/study?course_id=${encodeURIComponent(courseId || "")}`);
}

function formatIdentityTimestamp(value) {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? String(value || "Unknown") : timestamp.toLocaleString();
}

function renderPendingIdentities() {
  const list = el("pendingIdentityList");
  list.replaceChildren();
  el("pendingIdentityCount").textContent = `${state.pendingIdentityTotal} pending ${state.pendingIdentityTotal === 1 ? "identity" : "identities"}`;
  const pageNumber = Math.floor(state.pendingIdentityOffset / ADMIN_PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(state.pendingIdentityTotal / ADMIN_PAGE_SIZE));
  el("pendingPageStatus").textContent = `Page ${pageNumber} of ${pageCount}`;
  el("previousPendingPage").disabled = state.pendingIdentityOffset === 0;
  el("nextPendingPage").disabled = state.pendingIdentityOffset + state.pendingIdentities.length >= state.pendingIdentityTotal;

  if (!state.pendingIdentities.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No identities are waiting for approval.";
    list.appendChild(empty);
    return;
  }

  for (const identity of state.pendingIdentities) {
    const card = document.createElement("article");
    card.className = "pending-identity-card";
    const details = document.createElement("div");
    const name = document.createElement("h2");
    name.textContent = identity.display_name || "Unnamed Google identity";
    const email = document.createElement("p");
    email.className = "pending-identity-email";
    email.textContent = identity.email || "Email not provided";
    const issuer = document.createElement("code");
    issuer.textContent = `Issuer: ${identity.issuer}`;
    const subject = document.createElement("code");
    subject.textContent = `Subject: ${identity.subject}`;
    const seen = document.createElement("p");
    seen.className = "pending-identity-seen";
    seen.textContent = `Last sign-in attempt: ${formatIdentityTimestamp(identity.last_seen_at)}`;
    details.append(name, email, issuer, subject, seen);
    const actions = document.createElement("div");
    actions.className = "admin-card-actions";
    const approveLearner = document.createElement("button");
    approveLearner.type = "button";
    approveLearner.className = "primary";
    approveLearner.textContent = "Approve learner";
    approveLearner.addEventListener("click", () => {
      approvePendingIdentity(identity, "learner", approveLearner).catch((error) => showToast(error.message, "bad"));
    });
    const approveOwner = document.createElement("button");
    approveOwner.type = "button";
    approveOwner.className = "ghost";
    approveOwner.textContent = "Approve owner";
    approveOwner.addEventListener("click", () => {
      approvePendingIdentity(identity, "owner", approveOwner).catch((error) => showToast(error.message, "bad"));
    });
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "ghost danger-text";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", () => {
      dismissPendingIdentity(identity, dismiss).catch((error) => showToast(error.message, "bad"));
    });
    actions.append(approveLearner, approveOwner, dismiss);
    card.append(details, actions);
    list.appendChild(card);
  }
}

function renderApprovedIdentities() {
  const list = el("approvedIdentityList");
  list.replaceChildren();
  el("approvedIdentityCount").textContent = `${state.approvedIdentityTotal} approved ${state.approvedIdentityTotal === 1 ? "identity" : "identities"}`;
  const pageNumber = Math.floor(state.approvedIdentityOffset / ADMIN_PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(state.approvedIdentityTotal / ADMIN_PAGE_SIZE));
  el("approvedPageStatus").textContent = `Page ${pageNumber} of ${pageCount}`;
  el("previousApprovedPage").disabled = state.approvedIdentityOffset === 0;
  el("nextApprovedPage").disabled = state.approvedIdentityOffset + state.approvedIdentities.length >= state.approvedIdentityTotal;

  const filter = el("adminUserFilter").value.trim().toLowerCase();
  const visibleIdentities = state.approvedIdentities.filter((identity) => {
    if (!filter) return true;
    return [
      identity.display_name,
      identity.email,
      identity.label,
      identity.role,
      identity.enabled ? "enabled active" : "disabled restricted",
      identity.issuer,
      identity.subject
    ].some((value) => String(value || "").toLowerCase().includes(filter));
  });

  if (!visibleIdentities.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = filter
      ? "No users on this page match the filter."
      : "No approved identities were found.";
    list.appendChild(empty);
    return;
  }

  for (const identity of visibleIdentities) {
    const card = document.createElement("article");
    card.className = "pending-identity-card approved-identity-card";
    const details = document.createElement("div");
    const name = document.createElement("h3");
    name.textContent = identity.display_name || identity.label || "Approved identity";
    const email = document.createElement("p");
    email.className = "pending-identity-email";
    email.textContent = identity.email || "Email unavailable — ask this user to sign in again";
    const issuer = document.createElement("code");
    issuer.textContent = `Issuer: ${identity.issuer}`;
    const subject = document.createElement("code");
    subject.textContent = `Subject: ${identity.subject}`;
    const updated = document.createElement("p");
    updated.className = "pending-identity-seen";
    updated.textContent = `Updated: ${formatIdentityTimestamp(identity.updated_at)}`;
    details.append(name, email, issuer, subject, updated);
    const controls = document.createElement("div");
    controls.className = "identity-controls";
    const labelField = document.createElement("label");
    labelField.textContent = "Administrative label";
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.maxLength = 255;
    labelInput.value = identity.label || "";
    labelField.appendChild(labelInput);
    const roleField = document.createElement("label");
    roleField.textContent = "Role";
    const roleSelect = document.createElement("select");
    for (const value of ["learner", "owner"]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value === "owner" ? "Owner" : "Learner";
      option.selected = identity.role === value;
      roleSelect.appendChild(option);
    }
    roleField.appendChild(roleSelect);
    const enabledField = document.createElement("label");
    enabledField.className = "checkbox-control";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = Boolean(identity.enabled);
    enabledField.append(enabledInput, document.createTextNode(" Account enabled"));
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "Save identity";
    save.addEventListener("click", () => {
      saveApprovedIdentity(identity, {
        role: roleSelect.value,
        enabled: enabledInput.checked,
        label: labelInput.value
      }, save).catch((error) => showToast(error.message, "bad"));
    });

    const access = document.createElement("fieldset");
    access.className = "course-access-controls";
    const legend = document.createElement("legend");
    legend.textContent = "Course access";
    access.appendChild(legend);
    for (const courseId of state.courses.map((course) => course.course_id)) {
      const accessLabel = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = identity.course_access?.[courseId] !== false;
      checkbox.addEventListener("change", () => {
        updateCourseAccess(identity, courseId, checkbox.checked, checkbox)
          .catch((error) => showToast(error.message, "bad"));
      });
      accessLabel.append(checkbox, document.createTextNode(` ${courseId}`));
      access.appendChild(accessLabel);
    }
    controls.append(labelField, roleField, enabledField, save, access);
    card.append(details, controls);
    list.appendChild(card);
  }
  renderAdminAccessRoster();
  renderAdminAnalytics();
}

function identityDisplayName(identity) {
  return identity?.display_name || identity?.label || identity?.email || "Approved identity";
}

function selectAdminIdentity(identity) {
  state.adminSelectedIdentity = identity;
  renderAdminAccessRoster();
  renderAdminAccessWorkspace();
}

function renderAdminAccessRoster() {
  const roster = el("adminAccessRoster");
  if (!roster) return;
  roster.replaceChildren();
  const query = el("adminAccessFilter")?.value.trim().toLowerCase() || "";
  const identities = state.approvedIdentities.filter((identity) => {
    if (!query) return true;
    return [identityDisplayName(identity), identity.email, identity.role]
      .some((value) => String(value || "").toLowerCase().includes(query));
  });
  el("adminAccessUserCount").textContent = `${identities.length} shown`;
  for (const identity of identities) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "admin-roster-item";
    const selected = state.adminSelectedIdentity?.issuer === identity.issuer
      && state.adminSelectedIdentity?.subject === identity.subject;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
    const name = document.createElement("strong");
    name.textContent = identityDisplayName(identity);
    const meta = document.createElement("span");
    meta.textContent = `${identity.role === "owner" ? "Owner" : "Learner"} · ${identity.enabled ? "Active" : "Restricted"}`;
    button.append(name, meta);
    button.addEventListener("click", () => selectAdminIdentity(identity));
    roster.appendChild(button);
  }
  if (!identities.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No approved users match this filter.";
    roster.appendChild(empty);
  }
}

function renderAdminAccessWorkspace() {
  const identity = state.adminSelectedIdentity;
  const grid = el("adminAccessCourseGrid");
  if (!grid) return;
  grid.replaceChildren();
  el("adminAccessUserHeading").textContent = identity ? identityDisplayName(identity) : "Select a user";
  el("adminAccessUserRole").textContent = identity ? (identity.role === "owner" ? "Owner" : "Learner") : "";
  el("adminAccessDescription").textContent = identity
    ? (identity.email || "Email unavailable")
    : "Choose an approved user from the roster to configure course access.";
  if (!identity) return;
  for (const course of state.courses) {
    const courseId = course.course_id;
    const card = document.createElement("article");
    card.className = "admin-access-course-card";
    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = course.title || courseId;
    const meta = document.createElement("span");
    meta.textContent = courseId;
    text.append(title, meta);
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.setAttribute("aria-label", `${courseId} access for ${identityDisplayName(identity)}`);
    toggle.checked = identity.course_access?.[courseId] !== false;
    toggle.addEventListener("change", () => {
      updateCourseAccess(identity, courseId, toggle.checked, toggle)
        .then(() => renderAdminAccessWorkspace())
        .catch((error) => showToast(error.message, "bad"));
    });
    card.append(text, toggle);
    grid.appendChild(card);
  }
}

function renderAdminContent(courseId = state.adminSelectedContentCourseId) {
  const list = el("adminContentCourseList");
  if (!list) return;
  list.replaceChildren();
  el("adminContentCourseCount").textContent = `${state.courses.length} subjects`;
  for (const course of state.courses) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "admin-content-item";
    button.classList.toggle("is-selected", course.course_id === courseId);
    const title = document.createElement("strong");
    title.textContent = course.title || course.course_id;
    const meta = document.createElement("span");
    meta.textContent = course.study_available ? "Study and examination" : "Examination";
    button.append(title, meta);
    button.addEventListener("click", () => {
      state.adminSelectedContentCourseId = course.course_id;
      renderAdminContent(course.course_id);
    });
    list.appendChild(button);
  }
  const selected = state.courses.find((course) => course.course_id === courseId) || state.courses[0];
  if (!selected) return;
  state.adminSelectedContentCourseId = selected.course_id;
  el("adminContentPreviewHeading").textContent = selected.title || selected.course_id;
  el("adminContentPreviewDescription").textContent = selected.description || `${selected.course_id} installed course catalog.`;
  const metrics = el("adminContentMetricGrid");
  metrics.replaceChildren();
  const values = [
    ["Study guide", selected.study_available ? "Installed" : "Unavailable"],
    ["Practice", "Available"],
    ["Mock exam", "Available"]
  ];
  for (const [label, value] of values) {
    const card = document.createElement("article");
    const key = document.createElement("span");
    key.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    card.append(key, strong);
    metrics.appendChild(card);
  }
}

function renderAdminAnalytics() {
  if (!el("adminAnalyticsUsers")) return;
  el("adminAnalyticsUsers").textContent = String(state.approvedIdentityTotal || 0);
  el("adminAnalyticsPending").textContent = String(state.pendingIdentityTotal || 0);
  el("adminAnalyticsCourses").textContent = String(state.courses.length || 0);
}

function showAdminSection(panelId) {
  for (const panel of document.querySelectorAll(".admin-section-panel")) {
    panel.classList.toggle("hidden", panel.id !== panelId);
  }
  for (const tab of document.querySelectorAll(".admin-section-tab")) {
    const active = tab.dataset.adminSection === panelId;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-pressed", String(active));
  }
  if (panelId === "adminAccessPanel") {
    renderAdminAccessRoster();
    renderAdminAccessWorkspace();
  } else if (panelId === "adminContentPanel") {
    renderAdminContent();
  } else if (panelId === "adminAnalyticsPanel") {
    renderAdminAnalytics();
  }
}

async function loadPendingIdentities() {
  el("pendingIdentityStatus").textContent = "Loading pending identities…";
  el("refreshPendingButton").disabled = true;
  el("previousPendingPage").disabled = true;
  el("nextPendingPage").disabled = true;
  try {
    const payload = await api(
      `/api/admin/pending-identities?limit=${ADMIN_PAGE_SIZE}&offset=${state.pendingIdentityOffset}`
    );
    state.pendingIdentities = Array.isArray(payload.identities) ? payload.identities : [];
    state.pendingIdentityTotal = Number.isInteger(payload.total) ? payload.total : 0;
    if (state.pendingIdentityOffset > 0 && !state.pendingIdentities.length && state.pendingIdentityTotal > 0) {
      state.pendingIdentityOffset = Math.max(0, state.pendingIdentityOffset - ADMIN_PAGE_SIZE);
      await loadPendingIdentities();
      return;
    }
    el("pendingIdentityStatus").textContent = "Pending identities are validated Google sign-ins that still require owner approval.";
    renderPendingIdentities();
    renderAdminAnalytics();
  } catch (error) {
    state.pendingIdentities = [];
    state.pendingIdentityTotal = 0;
    el("pendingIdentityStatus").textContent = `Could not load pending identities: ${error.message}`;
    renderPendingIdentities();
    throw error;
  } finally {
    el("refreshPendingButton").disabled = false;
  }
}

async function loadApprovedIdentities() {
  el("approvedIdentityStatus").textContent = "Loading approved identities…";
  el("previousApprovedPage").disabled = true;
  el("nextApprovedPage").disabled = true;
  try {
    const payload = await api(
      `/api/admin/approved-identities?limit=${ADMIN_PAGE_SIZE}&offset=${state.approvedIdentityOffset}`
    );
    state.approvedIdentities = Array.isArray(payload.identities) ? payload.identities : [];
    state.approvedIdentityTotal = Number.isInteger(payload.total) ? payload.total : 0;
    if (state.approvedIdentityOffset > 0 && !state.approvedIdentities.length && state.approvedIdentityTotal > 0) {
      state.approvedIdentityOffset = Math.max(0, state.approvedIdentityOffset - ADMIN_PAGE_SIZE);
      await loadApprovedIdentities();
      return;
    }
    el("approvedIdentityStatus").textContent = "Roles are enforced from the exact issuer and subject shown below.";
    renderApprovedIdentities();
    if (state.adminSelectedIdentity) {
      state.adminSelectedIdentity = state.approvedIdentities.find((identity) =>
        identity.issuer === state.adminSelectedIdentity.issuer
        && identity.subject === state.adminSelectedIdentity.subject
      ) || null;
    }
    renderAdminAccessRoster();
    renderAdminAccessWorkspace();
  } catch (error) {
    state.approvedIdentities = [];
    state.approvedIdentityTotal = 0;
    el("approvedIdentityStatus").textContent = `Could not load approved identities: ${error.message}`;
    renderApprovedIdentities();
    throw error;
  }
}

async function openAdmin() {
  if (state.session?.user?.role !== "owner") {
    showToast("Owner authorization is required.", "bad");
    return;
  }
  showOnlyView("adminView");
  showAdminSection("adminUsersPanel");
  await Promise.all([loadPendingIdentities(), loadApprovedIdentities(), loadAdminAudit()]);
}

async function approvePendingIdentity(identity, role, button) {
  if (!window.confirm(`Approve this exact identity as ${role === "owner" ? "an owner" : "a learner"}?`)) return;
  button.disabled = true;
  try {
    await api("/api/admin/pending-identities/approve", {
      method: "POST",
      body: JSON.stringify({ issuer: identity.issuer, subject: identity.subject, role })
    });
    showToast(`Identity approved as ${role === "owner" ? "an owner" : "a learner"}.`, "ok");
    await Promise.all([loadPendingIdentities(), loadApprovedIdentities(), loadAdminAudit()]);
  } finally {
    button.disabled = false;
  }
}

async function dismissPendingIdentity(identity, button) {
  if (!window.confirm("Dismiss this request? The identity can appear again after another sign-in.")) return;
  button.disabled = true;
  try {
    await api("/api/admin/pending-identities/dismiss", {
      method: "POST",
      body: JSON.stringify({ issuer: identity.issuer, subject: identity.subject })
    });
    showToast("Pending request dismissed.", "ok");
    await Promise.all([loadPendingIdentities(), loadAdminAudit()]);
  } finally {
    button.disabled = false;
  }
}

async function saveApprovedIdentity(identity, changes, button) {
  const sensitiveChange = changes.role !== identity.role || changes.enabled !== identity.enabled;
  if (sensitiveChange && !window.confirm("Apply this role or account-status change?")) return;
  button.disabled = true;
  try {
    await api("/api/admin/approved-identities/update", {
      method: "POST",
      body: JSON.stringify({
        issuer: identity.issuer,
        subject: identity.subject,
        role: changes.role,
        enabled: changes.enabled,
        label: changes.label || null,
        updated_at: identity.updated_at
      })
    });
    showToast("Identity settings updated.", "ok");
    await Promise.all([loadApprovedIdentities(), loadAdminAudit()]);
  } finally {
    button.disabled = false;
  }
}

async function updateCourseAccess(identity, courseId, enabled, checkbox) {
  checkbox.disabled = true;
  try {
    await api("/api/admin/course-access/update", {
      method: "POST",
      body: JSON.stringify({
        issuer: identity.issuer,
        subject: identity.subject,
        course_id: courseId,
        enabled
      })
    });
    identity.course_access = { ...(identity.course_access || {}), [courseId]: enabled };
    showToast(`${courseId} access ${enabled ? "enabled" : "disabled"}.`, "ok");
    await loadAdminAudit();
  } catch (error) {
    checkbox.checked = !enabled;
    throw error;
  } finally {
    checkbox.disabled = false;
  }
}

function adminActionLabel(event) {
  const labels = {
    approve_pending_identity: "Approved identity",
    dismiss_pending_identity: "Dismissed request",
    update_identity: "Updated identity",
    update_course_access: "Updated course access"
  };
  return labels[event.action] || event.action;
}

function renderAdminAudit() {
  const list = el("adminAuditList");
  list.replaceChildren();
  el("adminAuditCount").textContent = `${state.adminAuditTotal} ${state.adminAuditTotal === 1 ? "event" : "events"}`;
  const pageNumber = Math.floor(state.adminAuditOffset / ADMIN_PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(state.adminAuditTotal / ADMIN_PAGE_SIZE));
  el("auditPageStatus").textContent = `Page ${pageNumber} of ${pageCount}`;
  el("previousAuditPage").disabled = state.adminAuditOffset === 0;
  el("nextAuditPage").disabled = state.adminAuditOffset + state.adminAuditEvents.length >= state.adminAuditTotal;
  for (const event of state.adminAuditEvents) {
    const row = document.createElement("article");
    row.className = "audit-row";
    const action = document.createElement("strong");
    action.textContent = adminActionLabel(event);
    const target = document.createElement("span");
    target.textContent = event.target_name || event.target_email || event.target_subject;
    const detail = document.createElement("span");
    const details = [];
    if (event.target_role) details.push(event.target_role);
    if (event.target_course_id) details.push(event.target_course_id);
    if (event.target_enabled !== null && event.target_enabled !== undefined) {
      details.push(event.target_enabled ? "enabled" : "disabled");
    }
    detail.textContent = details.join(" · ");
    const timestamp = document.createElement("time");
    timestamp.textContent = formatIdentityTimestamp(event.created_at);
    row.append(action, target, detail, timestamp);
    list.appendChild(row);
  }
  if (!state.adminAuditEvents.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No administrative actions have been recorded.";
    list.appendChild(empty);
  }
}

async function loadAdminAudit() {
  el("adminAuditStatus").textContent = "Loading activity…";
  const payload = await api(
    `/api/admin/audit-events?limit=${ADMIN_PAGE_SIZE}&offset=${state.adminAuditOffset}`
  );
  state.adminAuditEvents = Array.isArray(payload.events) ? payload.events : [];
  state.adminAuditTotal = Number.isInteger(payload.total) ? payload.total : 0;
  el("adminAuditStatus").textContent = "Newest successful owner actions appear first.";
  renderAdminAudit();
}

function openStudy() {
  if (!state.studyCatalog) {
    showToast("Study content is not available.", "bad");
    return;
  }
  el("studyGuideLabel").textContent = `${state.selectedCourseId || "Course"} study guide`;
  showOnlyView("studyView");
  renderChapters();
}

function statusLabel(status) {
  return String(status || "not_started").replaceAll("_", " ");
}

function statusClass(status) {
  return String(status || "not_started").toLowerCase().replace(/[^a-z0-9_-]/g, "");
}

function studyUnitLabels() {
  return state.currentChapter?.unit_labels || state.studyCatalog?.unit_labels || {
    singular: "Chapter",
    plural: "Chapters"
  };
}

function renderChapters() {
  const grid = el("chapterGrid");
  grid.replaceChildren();
  const unitLabels = studyUnitLabels();
  for (const chapter of state.studyCatalog?.chapters || []) {
    const card = document.createElement("article");
    card.className = `chapter-card status-${statusClass(chapter.status)}`;
    const number = document.createElement("span");
    number.className = "chapter-index";
    number.textContent = String(chapter.number).padStart(2, "0");
    const content = document.createElement("div");
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    const sourceContext = chapter.source_label || `pages ${chapter.page_start}-${chapter.page_end}`;
    eyebrow.textContent = `${unitLabels.singular} ${chapter.number} · ${sourceContext}`;
    const title = document.createElement("h2");
    title.textContent = chapter.title;
    const summary = document.createElement("p");
    summary.textContent = chapter.summary;
    const progress = document.createElement("p");
    progress.className = "chapter-progress";
    progress.textContent = `${statusLabel(chapter.status)} · ${chapter.lesson_count} lessons · ${chapter.mastered_flashcard_count}/${chapter.flashcard_count} flashcards mastered`;
    content.append(eyebrow, title, summary, progress);
    const action = document.createElement("button");
    action.type = "button";
    action.className = "ghost";
    action.textContent = chapter.status === "not_started"
      ? `Start ${unitLabels.singular.toLowerCase()}`
      : "Continue";
    action.addEventListener("click", () => {
      openChapter(chapter.chapter_id).catch((error) => showToast(error.message, "bad"));
    });
    card.append(number, content, action);
    grid.appendChild(card);
  }
}

async function openChapter(chapterId) {
  const isCurrentChapter = state.currentChapter?.chapter_id === chapterId;
  const payload = await api(
    `/api/study/chapters/${encodeURIComponent(chapterId)}?course_id=${encodeURIComponent(state.selectedCourseId)}`
  );
  state.currentChapter = payload.chapter;
  if (!isCurrentChapter) state.lessonIndex = 0;
  state.flashcardIndex = payload.chapter.resume_flashcard_index || 0;
  state.flashcardRevealed = false;
  if (state.currentChapter.status === "not_started") {
    const progress = await api(
      `/api/study/chapters/${encodeURIComponent(chapterId)}/progress`,
      {
        method: "POST",
        body: JSON.stringify({ course_id: state.selectedCourseId, status: "in_progress" })
      }
    );
    state.currentChapter.status = progress.progress.status;
    await loadStudyCatalog();
  }
  renderChapter();
  showOnlyView("chapterView");
}

function paragraphElements(text) {
  return String(text || "")
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
    .map((paragraph) => {
      const element = document.createElement("p");
      element.textContent = paragraph;
      return element;
    });
}

function citationText(citation) {
  const source = citation.source_title || citation.source_pdf || state.currentChapter?.source_pdf || "Course source";
  const locator = citation.locator || (
    citation.page_start && citation.page_end
      ? `pages ${citation.page_start}-${citation.page_end}`
      : citation.source_file
  );
  return `${source} - ${locator} - ${citation.source_file}`;
}

function renderEntryGroup(title, className, entries, fields) {
  if (!Array.isArray(entries) || !entries.length) return null;
  const section = document.createElement("section");
  section.className = `lesson-detail ${className}`;
  const heading = document.createElement("h3");
  heading.textContent = title;
  const grid = document.createElement("div");
  grid.className = "lesson-detail-grid";
  for (const entry of entries) {
    const card = document.createElement("article");
    for (const [field, label] of fields) {
      const value = entry[field];
      if (!value) continue;
      const paragraph = document.createElement("p");
      const strong = document.createElement("strong");
      strong.textContent = `${label}: `;
      paragraph.append(strong, document.createTextNode(value));
      card.appendChild(paragraph);
    }
    grid.appendChild(card);
  }
  section.append(heading, grid);
  return section;
}

function renderLessonVisual(visual) {
  const figure = document.createElement("figure");
  figure.className = `lesson-visual visual-${statusClass(visual.type)}`;
  const heading = document.createElement("h3");
  heading.textContent = visual.title;
  const description = document.createElement("p");
  description.textContent = visual.description;
  const grid = document.createElement("div");
  grid.className = "visual-grid";
  for (const item of visual.items) {
    const node = document.createElement("div");
    node.className = "visual-node";
    const label = document.createElement("strong");
    label.textContent = item.label;
    const detail = document.createElement("span");
    detail.textContent = item.detail;
    node.append(label, detail);
    grid.appendChild(node);
  }
  const caption = document.createElement("figcaption");
  caption.textContent = citationText(visual.citation);
  figure.append(heading, description, grid, caption);
  return figure;
}

function renderKnowledgeChecks(checks) {
  const section = document.createElement("section");
  section.className = "knowledge-checks";
  const heading = document.createElement("h3");
  heading.textContent = "Knowledge checks";
  section.appendChild(heading);
  for (const check of checks) {
    const item = document.createElement("article");
    const question = document.createElement("p");
    question.className = "knowledge-question";
    question.textContent = check.question;
    const answer = document.createElement("p");
    answer.className = "knowledge-answer hidden";
    answer.textContent = check.answer;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost knowledge-toggle";
    button.textContent = "Show answer";
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", () => {
      const revealed = !answer.classList.contains("hidden");
      answer.classList.toggle("hidden", revealed);
      button.textContent = revealed ? "Show answer" : "Hide answer";
      button.setAttribute("aria-expanded", String(!revealed));
    });
    item.append(question, button, answer);
    section.appendChild(item);
  }
  return section;
}

function renderLessonSection(section) {
  const article = document.createElement("section");
  article.className = "lesson-section active-lesson";
  article.append(...paragraphElements(section.body));

  const architecture = document.createElement("section");
  architecture.className = "architecture-context";
  const architectureHeading = document.createElement("h2");
  architectureHeading.textContent = "Architecture context";
  const architectureBody = document.createElement("p");
  architectureBody.textContent = section.architecture.context;
  const relationships = document.createElement("ul");
  for (const relationship of section.architecture.relationships) {
    const item = document.createElement("li");
    item.textContent = relationship;
    relationships.appendChild(item);
  }
  architecture.append(architectureHeading, architectureBody, relationships);
  article.appendChild(architecture);

  for (const visual of section.visuals || []) {
    article.appendChild(renderLessonVisual(visual));
  }

  const groups = [
    renderEntryGroup("Design decisions and trade-offs", "tradeoffs", section.tradeoffs, [
      ["decision", "Decision"], ["benefit", "Benefit"], ["cost", "Cost"]
    ]),
    renderEntryGroup("Practical security examples", "examples", section.examples, [
      ["title", "Example"], ["scenario", "Scenario"], ["analysis", "Why it matters"]
    ]),
    renderEntryGroup("Failure modes and misconceptions", "failure-modes", section.failure_modes, [
      ["mistake", "Mistake"], ["consequence", "Consequence"], ["correction", "Correction"]
    ]),
    renderEntryGroup("Important terminology", "terminology", section.terminology, [
      ["term", "Term"], ["definition", "Definition"]
    ])
  ];
  for (const group of groups) {
    if (group) article.appendChild(group);
  }

  const keyPoints = document.createElement("section");
  keyPoints.className = "lesson-key-points";
  const keyHeading = document.createElement("h2");
  keyHeading.textContent = "Key points";
  const points = document.createElement("ul");
  points.className = "key-points";
  for (const point of section.key_points) {
    const item = document.createElement("li");
    item.textContent = point;
    points.appendChild(item);
  }
  keyPoints.append(keyHeading, points);
  article.append(keyPoints, renderKnowledgeChecks(section.knowledge_checks));

  const sources = document.createElement("section");
  sources.className = "lesson-sources";
  const sourcesHeading = document.createElement("h2");
  sourcesHeading.textContent = "Sources";
  const citationList = document.createElement("ul");
  for (const citation of section.citations) {
    const item = document.createElement("li");
    item.textContent = citationText(citation);
    citationList.appendChild(item);
  }
  sources.append(sourcesHeading, citationList);
  if (section.references?.length) {
    const referenceHeading = document.createElement("h3");
    referenceHeading.textContent = "Supplemental references from the course material";
    const referenceList = document.createElement("ul");
    for (const reference of section.references) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = reference.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = reference.title;
      const sourceLocation = reference.source_locator || `PDF page ${reference.source_page}`;
      item.append(link, document.createTextNode(` - ${reference.role}; cited at ${sourceLocation}`));
      referenceList.appendChild(item);
    }
    sources.append(referenceHeading, referenceList);
  }
  article.appendChild(sources);
  return article;
}

function renderChapter() {
  const chapter = state.currentChapter;
  if (!chapter) return;
  const unitLabels = studyUnitLabels();
  const unitSingularLower = unitLabels.singular.toLowerCase();
  const lessons = chapter.sections || [];
  const flashcards = chapter.flashcards || [];
  const masteredCards = flashcards.filter((card) => card.state === "mastered").length;
  state.lessonIndex = Math.max(0, Math.min(state.lessonIndex, Math.max(0, lessons.length - 1)));

  el("chapterBreadcrumb").textContent = `${unitLabels.singular} ${chapter.number}`;
  const sourceContext = chapter.source_label || `pages ${chapter.page_start}-${chapter.page_end}`;
  el("chapterNumber").textContent = `${unitLabels.singular} ${chapter.number} · ${sourceContext}`;
  el("chapterTitle").textContent = chapter.title;
  el("chapterSummary").textContent = chapter.summary;
  el("chapterStatus").textContent = statusLabel(chapter.status);
  el("chapterStatus").className = `status-pill status-${statusClass(chapter.status)}`;
  el("chapterOutcomesLabel").textContent = `${unitLabels.singular} outcomes`;
  el("openGroundedLabel").textContent = `Ask this ${unitSingularLower}`;
  el("openGroundedDescription").textContent = `Search the ${unitSingularLower}'s indexed evidence`;
  el("lessonBackButton").textContent = `${unitLabels.singular} overview`;
  el("lessonContentsEyebrow").textContent = `In this ${unitSingularLower}`;
  el("flashcardBackButton").textContent = `${unitLabels.singular} overview`;
  el("groundedBackButton").textContent = `${unitLabels.singular} overview`;
  el("chapterLessonCount").textContent = `${lessons.length} ${lessons.length === 1 ? "lesson" : "lessons"}`;
  el("chapterFlashcardSummary").textContent = `${masteredCards} of ${flashcards.length} cards mastered`;
  el("completeChapterButton").disabled = chapter.status === "completed";
  el("completeChapterButton").textContent = chapter.status === "completed"
    ? `${unitLabels.singular} completed`
    : `Mark ${unitLabels.singular.toLowerCase()} complete`;
  el("continueLessonButton").disabled = lessons.length === 0;
  el("openFlashcardsButton").disabled = flashcards.length === 0;

  const objectives = el("chapterObjectives");
  objectives.replaceChildren();
  for (const objective of chapter.objectives) {
    const item = document.createElement("li");
    item.textContent = objective;
    objectives.appendChild(item);
  }

  const lessonList = el("lessonList");
  lessonList.replaceChildren();
  lessons.forEach((lesson, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lesson-list-item";
    const number = document.createElement("span");
    number.className = "lesson-list-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const content = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = lesson.heading;
    const meta = document.createElement("small");
    const checkCount = lesson.knowledge_checks?.length || 0;
    meta.textContent = `${checkCount} knowledge ${checkCount === 1 ? "check" : "checks"} · cited lesson`;
    content.append(title, meta);
    const arrow = document.createElement("span");
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "→";
    button.append(number, content, arrow);
    button.addEventListener("click", () => openLesson(index));
    lessonList.appendChild(button);
  });
}

function showChapterOverview() {
  if (!state.currentChapter) return;
  renderChapter();
  showOnlyView("chapterView");
}

function currentLesson() {
  return state.currentChapter?.sections?.[state.lessonIndex] || null;
}

function openLesson(index = state.lessonIndex) {
  const lessons = state.currentChapter?.sections || [];
  if (!lessons.length) {
    showToast(`This ${studyUnitLabels().singular.toLowerCase()} does not contain any lessons.`, "bad");
    return;
  }
  state.lessonIndex = Math.max(0, Math.min(lessons.length - 1, index));
  renderLesson();
  showOnlyView("lessonView");
}

function renderLesson() {
  const chapter = state.currentChapter;
  const lesson = currentLesson();
  if (!chapter || !lesson) return;
  const lessons = chapter.sections;
  el("lessonBreadcrumbChapter").textContent = `${studyUnitLabels().singular} ${chapter.number}`;
  el("lessonBreadcrumbPosition").textContent = `Lesson ${state.lessonIndex + 1}`;
  el("lessonPosition").textContent = `Lesson ${state.lessonIndex + 1} of ${lessons.length}`;
  el("lessonTitle").textContent = lesson.heading;
  el("lessonChapterTitle").textContent = chapter.title;
  el("previousLesson").disabled = state.lessonIndex === 0;
  el("nextLesson").textContent = state.lessonIndex === lessons.length - 1
    ? `Back to ${studyUnitLabels().singular.toLowerCase()} overview`
    : "Next lesson";
  el("lessonContent").replaceChildren(renderLessonSection(lesson));

  const contents = el("lessonContents");
  contents.replaceChildren();
  lessons.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lesson-content-link";
    button.textContent = `${index + 1}. ${item.heading}`;
    if (index === state.lessonIndex) {
      button.classList.add("active");
      button.setAttribute("aria-current", "page");
    }
    button.addEventListener("click", () => openLesson(index));
    contents.appendChild(button);
  });
  renderLessonBookmark();
}

function renderLessonBookmark() {
  const lesson = currentLesson();
  const button = el("lessonBookmarkButton");
  if (!lesson) {
    button.disabled = true;
    return;
  }
  const bookmarked = state.learnerItems.some(
    (item) => item.item_type === "lesson" && item.item_id === lesson.section_id
  );
  button.disabled = false;
  button.textContent = bookmarked ? "Remove bookmark" : "Bookmark lesson";
  button.setAttribute("aria-pressed", String(bookmarked));
}

async function toggleLessonBookmark() {
  const lesson = currentLesson();
  if (!lesson) return;
  const bookmarked = state.learnerItems.some(
    (item) => item.item_type === "lesson" && item.item_id === lesson.section_id
  );
  await updateLearnerItem("lesson", lesson.section_id, !bookmarked);
}

function moveLesson(direction) {
  const lessons = state.currentChapter?.sections || [];
  const nextIndex = state.lessonIndex + direction;
  if (nextIndex >= lessons.length) {
    showChapterOverview();
    return;
  }
  openLesson(nextIndex);
}

function openFlashcards() {
  const chapter = state.currentChapter;
  if (!chapter?.flashcards?.length) {
    showToast(`This ${studyUnitLabels().singular.toLowerCase()} does not contain any flashcards.`, "bad");
    return;
  }
  el("flashcardBreadcrumbChapter").textContent = `${studyUnitLabels().singular} ${chapter.number}`;
  el("flashcardViewTitle").textContent = `${chapter.title} flashcards`;
  renderFlashcard();
  showOnlyView("flashcardView");
}

function openGroundedExplanation() {
  const chapter = state.currentChapter;
  if (!chapter) return;
  el("groundedBreadcrumbChapter").textContent = `${studyUnitLabels().singular} ${chapter.number}`;
  el("groundedViewTitle").textContent = `Ask ${studyUnitLabels().singular} ${chapter.number}`;
  el("groundedViewDescription").textContent = `Search indexed evidence from ${chapter.title}.`;
  el("groundedQuestion").value = "";
  el("groundedAnswer").replaceChildren();
  hideEl("groundedAnswer");
  showOnlyView("groundedView");
}

function currentFlashcard() {
  return state.currentChapter?.flashcards?.[state.flashcardIndex] || null;
}

function renderFlashcard() {
  const card = currentFlashcard();
  if (!card) return;
  const total = state.currentChapter.flashcards.length;
  el("flashcardCount").textContent = `${state.flashcardIndex + 1} / ${total}`;
  el("flashcardState").textContent = statusLabel(card.state);
  el("flashcardState").className = `status-pill status-${statusClass(card.state)}`;
  el("flashcardPrompt").textContent = card.front;
  el("flashcardAnswer").textContent = card.back;
  el("flashcardAnswer").classList.toggle("hidden", !state.flashcardRevealed);
  el("flashcardRevealHint").classList.toggle("hidden", state.flashcardRevealed);
  el("flashcardAssessment").classList.toggle("hidden", !state.flashcardRevealed);
  el("previousFlashcard").disabled = state.flashcardIndex === 0;
  el("nextFlashcard").disabled = state.flashcardIndex >= total - 1;
  const citation = card.citation;
  el("flashcardCitation").textContent = citationText(citation);
  el("flashcard").setAttribute(
    "aria-label",
    state.flashcardRevealed ? "Flashcard answer revealed" : "Reveal flashcard answer"
  );
  el("flashcard").setAttribute("aria-expanded", String(state.flashcardRevealed));
}

function moveFlashcard(direction) {
  const total = state.currentChapter?.flashcards?.length || 0;
  state.flashcardIndex = Math.max(0, Math.min(total - 1, state.flashcardIndex + direction));
  state.flashcardRevealed = false;
  renderFlashcard();
}

async function reviewFlashcard(known) {
  const card = currentFlashcard();
  if (!card) return;
  const payload = await api(
    `/api/study/flashcards/${encodeURIComponent(card.flashcard_id)}/review`,
    {
      method: "POST",
      body: JSON.stringify({ course_id: state.selectedCourseId, known })
    }
  );
  Object.assign(card, payload.progress);
  renderFlashcard();
  showToast(known ? "Flashcard marked as known." : "Flashcard kept in review.", known ? "ok" : "neutral");
  if (state.flashcardIndex < state.currentChapter.flashcards.length - 1) {
    moveFlashcard(1);
  }
  await loadStudyCatalog();
}

async function completeChapter() {
  const chapter = state.currentChapter;
  if (!chapter || chapter.status === "completed") return;
  const payload = await api(
    `/api/study/chapters/${encodeURIComponent(chapter.chapter_id)}/progress`,
    {
      method: "POST",
      body: JSON.stringify({ course_id: state.selectedCourseId, status: "completed" })
    }
  );
  chapter.status = payload.progress.status;
  renderChapter();
  await loadStudyCatalog();
  showToast(`${studyUnitLabels().singular} marked complete.`, "ok");
}

async function askGroundedQuestion(event) {
  event.preventDefault();
  const question = el("groundedQuestion").value.trim();
  if (!question || !state.currentChapter) return;
  const submit = event.submitter;
  const originalLabel = submit?.textContent || "Find evidence";
  if (submit) {
    submit.disabled = true;
    submit.textContent = "Searching…";
  }
  try {
    const payload = await api("/api/study/explain", {
      method: "POST",
      body: JSON.stringify({
        course_id: state.selectedCourseId,
        chapter_id: state.currentChapter.chapter_id,
        question
      })
    });
    const answer = el("groundedAnswer");
    answer.replaceChildren();
    const message = document.createElement("p");
    message.textContent = payload.message;
    answer.appendChild(message);
    for (const evidence of payload.evidence || []) {
      const item = document.createElement("article");
      const snippet = document.createElement("p");
      snippet.textContent = evidence.snippet;
      const citation = document.createElement("p");
      citation.className = "citation";
      citation.textContent = citationText(evidence);
      item.append(snippet, citation);
      answer.appendChild(item);
    }
    showEl("groundedAnswer");
  } catch (error) {
    showToast(error.message, "bad");
  } finally {
    if (submit) {
      submit.disabled = false;
      submit.textContent = originalLabel;
    }
  }
}

/* ============================================================
   AUTH / SESSION
   ============================================================ */
function showSignedOut() {
  state.session = null;
  hideEl("applicationMain");
  showEl("loginRequired");
  hideEl("logoutButton");
  hideEl("adminButton");
  hideEl("globalSearchButton");
  hideEl("appNavigation");
  hideEl("searchOverlay");
  el("userIdentity").textContent = "";
}

function showSignedIn(session) {
  state.session = session;
  const user = session.user;
  showEl("applicationMain");
  hideEl("loginRequired");
  showEl("logoutButton");
  showEl("globalSearchButton");
  showEl("appNavigation");
  el("adminButton").classList.toggle("hidden", user.role !== "owner");
  el("userIdentity").textContent = user.display_name || user.email || "Approved learner";
  showCourseSelection();
}

/* ============================================================
   HELPERS
   ============================================================ */
function stopTimer() {
  if (state.timerId) {
    window.clearInterval(state.timerId);
  }
  state.timerId = null;
}

function clearTimer() {
  stopTimer();
  state.deadline = null;
}

function currentQuestion() {
  return state.questions[state.index];
}

function selectedLabels(question) {
  return state.answers[question.question_id] || [];
}

/* ============================================================
   MODE SWITCHING
   ============================================================ */
async function startSession(mode, bundleId) {
  if (state.sessionStartState === "loading") return;
  state.bundleSelectionMode = mode;
  state.sessionStartState = "loading";
  state.sessionStartError = "";
  state.startingBundleId = bundleId;
  if (mode === "practice") {
    state.selectedPracticeBundleId = bundleId;
  } else {
    state.selectedExamBundleId = bundleId;
  }

  if (el("bundleSelectionView").classList.contains("hidden")) {
    renderExamPathway();
  } else {
    renderBundleSelection();
  }

  try {
    await setMode(mode);
    state.sessionStartState = "idle";
    state.sessionStartError = "";
    state.startingBundleId = null;
  } catch (error) {
    state.sessionStartState = "error";
    const recovery = bundlesForMode(mode).length
      ? "Choose the bundle again or return to the examination module."
      : "Try again or return to the course pathways.";
    state.sessionStartError = `We couldn't start this assessment. ${error.message} ${recovery}`;
    state.startingBundleId = null;
    if (el("bundleSelectionView").classList.contains("hidden")) {
      renderExamPathway();
    } else {
      renderBundleSelection();
    }
    throw error;
  }
}

async function setMode(mode) {
  state.mode = mode;
  state.answers = {};
  state.practiceResults = {};
  state.index = 0;
  state.attemptId = null;
  clearTimer();
  state.examStatus = "idle";

  if (mode === "exam") {
    const payload = await api("/api/exam/start", {
      method: "POST",
      body: JSON.stringify({
        course_id: state.selectedCourseId,
        bundle_id: state.selectedExamBundleId
      })
    });
    state.attemptId = payload.attempt_id;
    state.payload = { ...state.payload, exam: payload.exam };
    state.questions = payload.questions;
    state.examStatus = "active";
    state.deadline = new Date(payload.attempt.deadline_at).getTime();
    if (Number.isNaN(state.deadline)) {
      state.deadline = Date.now() + state.payload.exam.duration_seconds * 1000;
    }
    state.timerId = window.setInterval(renderTimer, 1000);
  } else if ((state.payload.practice?.bundles || []).length) {
    const payload = await api("/api/practice/start", {
      method: "POST",
      body: JSON.stringify({
        course_id: state.selectedCourseId,
        bundle_id: state.selectedPracticeBundleId
      })
    });
    state.attemptId = payload.attempt_id;
    state.payload = { ...state.payload, practice: payload.practice };
    state.questions = payload.questions;
  } else {
    state.questions = state.payload.questions;
  }

  el("submitAnswer").textContent = mode === "exam" ? "Save Answer" : "Submit Answer";
  el("finishExam").classList.toggle("hidden", mode !== "exam");

  showQuestionView();
  render();
}

/* ============================================================
   TIMER
   ============================================================ */
function setTimerText(value) {
  el("timerMetric").textContent = value;
  el("mobileTimerMetric").textContent = value;
}

function renderTimer() {
  if (state.mode !== "exam") {
    setTimerText("Unlimited");
    return;
  }
  if (state.examStatus === "completed") {
    setTimerText("Completed");
    return;
  }
  if (state.examStatus === "submitting") {
    setTimerText("Submitting…");
    return;
  }
  if (!state.deadline) {
    setTimerText("--:--");
    return;
  }

  const remaining = Math.max(0, Math.floor((state.deadline - Date.now()) / 1000));
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const seconds = remaining % 60;
  const display = `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  setTimerText(display);

  if (remaining < 300 && remaining > 0) {
    el("timerMetric").style.color = "var(--bad)";
    el("mobileTimerMetric").style.color = "var(--bad)";
  } else {
    el("timerMetric").style.color = "";
    el("mobileTimerMetric").style.color = "";
  }

  if (remaining === 0 && state.timerId) {
    stopTimer();
    finishExam().catch((error) => showToast(error.message, "bad"));
  }
}

/* ============================================================
   RENDER QUESTION VIEW
   ============================================================ */
function render() {
  if (!state.payload || !state.questions.length) {
    renderSkeletons();
    return;
  }
  const question = currentQuestion();
  if (!question) return;

  const examSubmitting = state.mode === "exam" && state.examStatus === "submitting";
  const examCompleted = state.mode === "exam" && state.examStatus === "completed";
  const examLocked = state.mode === "exam" && state.examStatus !== "active";
  const practiceAnswered = state.mode === "practice" && Boolean(state.practiceResults[question.question_id]);

  el("questionModeLabel").textContent = state.mode === "exam"
    ? (state.payload.exam.selected_bundle?.label || state.payload.exam.label || "Exam")
    : (state.payload.practice?.selected_bundle?.label || state.payload.practice?.label || "Practice");
  el("questionText").textContent = question.question;
  el("questionCount").textContent = `Question ${state.index + 1} of ${state.questions.length}`;
  el("sourceText").textContent = question.page_range ? `${question.source_file}, pages ${question.page_range}` : question.source_file;
  renderChoices(question, examLocked || practiceAnswered);
  renderFeedback(question);

  const notice = el("examNotice");
  const shortage = state.payload.exam.target_question_count - state.payload.exam.available_question_count;
  if (state.mode === "exam" && shortage > 0) {
    notice.textContent = `${state.payload.exam.label || "Exam"} targets ${state.payload.exam.target_question_count} questions. This run uses the ${state.payload.exam.available_question_count} questions currently in the bank.`;
    notice.classList.remove("hidden");
  } else {
    notice.classList.add("hidden");
  }

  el("previousQuestion").disabled = examSubmitting || state.index === 0;
  el("nextQuestion").disabled = examSubmitting || state.index >= state.questions.length - 1;
  el("submitAnswer").disabled = examSubmitting || examCompleted || practiceAnswered;
  el("finishExam").disabled = examSubmitting || examCompleted;
  el("finishExam").textContent = examSubmitting ? "Submitting…" : examCompleted ? "Exam Completed" : "Finish Exam";
  renderTimer();
  renderMetrics();
  renderQuestionMap();
  renderMeta();
}

function renderSkeletons() {
  const choices = el("choices");
  choices.replaceChildren();
  for (let i = 0; i < 4; i++) {
    const skel = document.createElement("div");
    skel.className = "skeleton";
    skel.style.height = "54px";
    skel.style.marginBottom = "8px";
    skel.style.borderRadius = "var(--radius-sm)";
    choices.appendChild(skel);
  }
  el("questionText").textContent = "Loading questions…";
  el("questionCount").textContent = "—";
  el("sourceText").textContent = "Please wait";
}

function renderChoices(question, examLocked) {
  const choices = el("choices");
  choices.replaceChildren();
  const selected = new Set(selectedLabels(question));
  const inputType = question.multi_select ? "checkbox" : "radio";

  for (const choice of question.choices) {
    const label = document.createElement("label");
    label.className = `choice${selected.has(choice.label) ? " selected" : ""}`;
    if (examLocked) label.classList.add("locked");

    const input = document.createElement("input");
    input.type = inputType;
    input.name = "choice";
    input.value = choice.label;
    input.checked = selected.has(choice.label);
    input.disabled = examLocked;

    const choiceText = document.createElement("span");
    const choiceLabel = document.createElement("span");
    choiceLabel.className = "choice-label";
    choiceLabel.textContent = `${choice.label}.`;
    choiceText.append(choiceLabel, document.createTextNode(choice.text));
    label.append(input, choiceText);
    label.addEventListener("change", saveSelection);
    choices.appendChild(label);
  }
}

function renderFeedback(question) {
  const feedback = el("feedback");
  const result = state.practiceResults[question.question_id];
  feedback.className = "feedback";
  feedback.textContent = "";
  if (!result) return;

  if (result.is_correct === true) {
    feedback.classList.add("ok");
    feedback.textContent = "Correct.";
  } else if (result.is_correct === false) {
    feedback.classList.add("bad");
    feedback.textContent = `Incorrect. Correct answer: ${result.correct_answer_text || "not available"}.`;
  } else {
    feedback.classList.add("neutral");
    feedback.textContent = "This question has no source-provided answer.";
  }
  if (result.explanation) {
    feedback.textContent += ` ${result.explanation}`;
  }
}

function renderMetrics() {
  const answered = Object.values(state.answers).filter((labels) => labels.length > 0).length;
  const total = state.questions.length || 1;
  el("modeMetric").textContent = state.mode === "exam" ? "Exam" : "Practice";
  el("answeredMetric").textContent = `${answered}/${total}`;
  el("mobileAnsweredMetric").textContent = `${answered}/${total}`;
  const graded = Object.values(state.practiceResults).filter((result) => result.is_correct !== null);
  const correct = graded.filter((result) => result.is_correct === true).length;
  el("scoreMetric").textContent = graded.length ? `${Math.round((correct / graded.length) * 100)}%` : "0%";
}

function renderQuestionMap() {
  const map = el("questionMap");
  map.replaceChildren();
  state.questions.forEach((question, index) => {
    const button = document.createElement("button");
    button.className = "jump";
    if (index === state.index) button.classList.add("current");
    if ((state.answers[question.question_id] || []).length) button.classList.add("answered");
    button.type = "button";
    button.textContent = String(index + 1);
    button.disabled = state.mode === "exam" && state.examStatus === "submitting";
    button.addEventListener("click", () => {
      state.index = index;
      closeQuestionMap();
      render();
    });
    map.appendChild(button);
  });
}

function renderMeta() {
  if (state.mode !== "exam") {
    const practice = state.payload.practice || {};
    el("sessionFormatTitle").textContent = "Practice Format";
    const selectedBundle = practice.selected_bundle;
    const label = selectedBundle?.label || practice.label || "Practice";
    el("examMeta").textContent = `${label}: ${state.questions.length} questions, unlimited time. Available now: ${practice.available_question_count || state.questions.length}.`;
    return;
  }
  const exam = state.payload.exam;
  const minutes = Math.round(exam.duration_seconds / 60);
  const selectedBundle = exam.selected_bundle;
  const label = selectedBundle?.label || exam.label || "Exam";
  el("sessionFormatTitle").textContent = "Exam Format";
  el("examMeta").textContent = `${label}: ${state.questions.length} questions, ${minutes} minutes, ${exam.passing_score_percent}% passing score. Available now: ${exam.available_question_count}.`;
}

/* ============================================================
   SAVE SELECTION
   ============================================================ */
function saveSelection() {
  if (state.mode === "exam" && state.examStatus !== "active") return;
  const question = currentQuestion();
  if (!question) return;
  const checked = [...document.querySelectorAll("input[name='choice']:checked")].map((input) => input.value);
  state.answers[question.question_id] = checked;
  renderMetrics();
  renderQuestionMap();
}

/* ============================================================
   SUBMIT ANSWER
   ============================================================ */
async function submitAnswer() {
  if (state.mode === "exam" && state.examStatus !== "active") return;
  saveSelection();
  const question = currentQuestion();
  const selected = selectedLabels(question);
  if (!selected.length) {
    showToast("Choose at least one answer first.", "neutral");
    return;
  }
  if (state.mode === "exam") {
    if (state.index < state.questions.length - 1) {
      state.index += 1;
    }
    render();
    return;
  }
  try {
    const result = await api("/api/answer", {
      method: "POST",
      body: JSON.stringify({
        course_id: state.selectedCourseId,
        attempt_id: state.attemptId,
        question_id: question.question_id,
        selected
      })
    });
    state.practiceResults[question.question_id] = result;
    render();
    if (result.is_correct) {
      showToast("Correct.", "ok");
    } else {
      showToast("Incorrect. See feedback below.", "bad");
    }
    if (result.attempt_progress?.completed && state.attemptId) {
      state.lastAssessment = {
        mode: "practice",
        bundleId: state.selectedPracticeBundleId
      };
      await loadAttemptDetail(state.attemptId);
    }
  } catch (error) {
    showToast(error.message, "bad");
  }
}

/* ============================================================
   FINISH EXAM
   ============================================================ */
async function finishExam() {
  if (state.mode !== "exam" || state.examStatus !== "active") return;
  saveSelection();
  state.examStatus = "submitting";
  stopTimer();
  render();

  const overlay = el("loadingOverlay");
  overlay.classList.remove("hidden");

  const answers = {};
  for (const question of state.questions) {
    answers[question.question_id] = state.answers[question.question_id] || [];
  }

  try {
    const result = await api("/api/score", {
      method: "POST",
      body: JSON.stringify({ course_id: state.selectedCourseId, attempt_id: state.attemptId, answers })
    });
    state.examStatus = "completed";
    state.deadline = null;
    state.practiceResults = Object.fromEntries(result.results.map((item) => [item.question_id, item]));
    render();
    const status = result.passed ? "passed" : "did not pass";
    const msg = `Exam complete: ${result.correct_count}/${result.graded_count} correct (${result.percent || 0}%). You ${status}. Passing score is ${result.passing_score_percent}%.`;
    showToast(msg, result.passed ? "ok" : "bad");
    const fb = el("feedback");
    fb.className = `feedback ${result.passed ? "ok" : "bad"}`;
    fb.textContent = msg;
    state.lastAssessment = {
      mode: "exam",
      bundleId: state.selectedExamBundleId
    };
    await loadAttemptDetail(state.attemptId);
  } catch (error) {
    state.examStatus = "active";
    if (state.deadline && state.deadline > Date.now()) {
      state.timerId = window.setInterval(renderTimer, 1000);
    }
    render();
    showToast(error.message, "bad");
  } finally {
    overlay.classList.add("hidden");
  }
}

/* ============================================================
   ASSESSMENT RESULTS
   ============================================================ */
async function loadAttemptDetail(attemptId) {
  state.currentResults = await api(
    `/api/attempt-detail?attempt_id=${encodeURIComponent(attemptId)}`
  );
  state.resultFilter = "all";
  renderResults();
  showOnlyView("resultsView");
  await loadDashboard();
}

function resultChoiceText(result, labels) {
  const choices = new Map((result.choices || []).map((choice) => [choice.label, choice.text]));
  if (!labels?.length) return "Unanswered";
  return labels.map((label) => `${label}. ${choices.get(label) || "Unknown choice"}`).join("; ");
}

function renderResults() {
  const payload = state.currentResults;
  if (!payload) return;
  const attempt = payload.attempt;
  const summary = payload.summary;
  const isExam = attempt.assessment_kind === "exam";
  el("resultsEyebrow").textContent = isExam ? "Mock exam complete" : "Practice bundle complete";
  el("resultsTitle").textContent = attemptLabel(attempt);
  el("resultsSubtitle").textContent = `${summary.question_count} questions · Completed ${formatIdentityTimestamp(attempt.submitted_at)}`;
  el("resultsScore").textContent = formatScore(summary.percent);
  el("resultsCorrect").textContent = `${summary.correct_count}/${summary.question_count}`;
  el("resultsIncorrect").textContent = String(summary.question_count - summary.correct_count);
  el("resultsStatus").textContent = isExam
    ? (summary.passed ? "Passed" : "Not passed")
    : "Completed";
  el("resultsStatus").className = isExam && !summary.passed ? "result-bad" : "result-good";

  for (const button of document.querySelectorAll(".result-filter")) {
    const active = button.dataset.resultFilter === state.resultFilter;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }

  const list = el("resultQuestionList");
  list.replaceChildren();
  const visible = payload.results.filter((result) => {
    if (state.resultFilter === "incorrect") return result.is_correct === false;
    if (state.resultFilter === "unanswered") return !result.selected?.length;
    return true;
  });
  for (const result of visible) {
    const card = document.createElement("article");
    card.className = `result-question-card ${result.is_correct ? "result-correct" : "result-incorrect"}`;
    const header = document.createElement("div");
    header.className = "section-heading-row";
    const title = document.createElement("h2");
    title.textContent = `Question ${result.position + 1}`;
    const status = document.createElement("span");
    status.className = `status-pill ${result.is_correct ? "status-completed" : ""}`;
    status.textContent = result.is_correct ? "Correct" : result.selected.length ? "Incorrect" : "Unanswered";
    header.append(title, status);
    const question = document.createElement("p");
    question.className = "result-question-text";
    question.textContent = result.question;
    const selected = document.createElement("p");
    selected.append(
      Object.assign(document.createElement("strong"), { textContent: "Your answer: " }),
      document.createTextNode(resultChoiceText(result, result.selected))
    );
    const correct = document.createElement("p");
    correct.append(
      Object.assign(document.createElement("strong"), { textContent: "Correct answer: " }),
      document.createTextNode(resultChoiceText(result, result.correct_answer))
    );
    const explanation = document.createElement("p");
    explanation.className = "result-explanation";
    explanation.textContent = result.explanation || "No explanation is available.";
    const source = document.createElement("p");
    source.className = "citation";
    source.textContent = result.page_range
      ? `${result.source_file}, pages ${result.page_range}`
      : result.source_file;
    const review = document.createElement("button");
    review.type = "button";
    review.className = "ghost";
    review.textContent = result.in_review_queue ? "Remove from review queue" : "Add to review queue";
    review.addEventListener("click", () => {
      updateLearnerItem("question", result.question_id, !result.in_review_queue)
        .catch((error) => showToast(error.message, "bad"));
    });
    card.append(header, question, selected, correct, explanation, source, review);
    list.appendChild(card);
  }
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No questions match this filter.";
    list.appendChild(empty);
  }
}

function retakeCurrentAssessment() {
  const attempt = state.currentResults?.attempt;
  if (!attempt) return;
  const mode = attempt.assessment_kind === "practice" ? "practice" : "exam";
  const bundleId = String(attempt.content_version || "").replace(/^practice:/, "");
  if (mode === "practice") state.selectedPracticeBundleId = bundleId;
  else state.selectedExamBundleId = bundleId;
  startSession(mode, bundleId).catch((error) => showToast(error.message, "bad"));
}

/* ============================================================
   GLOBAL SEARCH
   ============================================================ */
function openGlobalSearch() {
  if (!state.selectedCourseId) {
    showToast("Open a course before searching.", "neutral");
    return;
  }
  showEl("searchOverlay");
  el("globalSearchInput").focus();
}

function closeGlobalSearch() {
  hideEl("searchOverlay");
  el("globalSearchButton").focus();
}

function renderSearchResults() {
  const list = el("globalSearchResults");
  list.replaceChildren();
  for (const result of state.searchResults) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-result";
    const type = document.createElement("span");
    type.className = "eyebrow";
    type.textContent = result.type;
    const title = document.createElement("strong");
    title.textContent = result.title;
    const subtitle = document.createElement("span");
    subtitle.textContent = result.subtitle || "";
    button.append(type, title, subtitle);
    button.addEventListener("click", () => {
      activateSearchResult(result).catch((error) => showToast(error.message, "bad"));
    });
    list.appendChild(button);
  }
  if (!state.searchResults.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No matching study content or bundles were found.";
    list.appendChild(empty);
  }
}

async function searchCurrentCourse(event) {
  event.preventDefault();
  const query = el("globalSearchInput").value.trim();
  if (query.length < 2) return;
  el("globalSearchStatus").textContent = "Searching…";
  const payload = await api(
    `/api/search?course_id=${encodeURIComponent(state.selectedCourseId)}&q=${encodeURIComponent(query)}`
  );
  state.searchResults = Array.isArray(payload.results) ? payload.results : [];
  el("globalSearchStatus").textContent = `${state.searchResults.length} ${state.searchResults.length === 1 ? "result" : "results"}`;
  renderSearchResults();
}

async function activateSearchResult(result) {
  closeGlobalSearch();
  if (result.type === "chapter") {
    await openChapter(result.id);
    return;
  }
  if (result.type === "lesson") {
    await openChapter(result.chapter_id);
    const index = (state.currentChapter?.sections || []).findIndex(
      (section) => section.section_id === result.id
    );
    openLesson(index >= 0 ? index : 0);
    return;
  }
  if (result.type === "flashcard") {
    await openChapter(result.chapter_id);
    const index = (state.currentChapter?.flashcards || []).findIndex(
      (card) => card.flashcard_id === result.id
    );
    state.flashcardIndex = index >= 0 ? index : 0;
    openFlashcards();
    return;
  }
  if (result.type === "bundle") {
    if (result.mode === "practice") state.selectedPracticeBundleId = result.id;
    else state.selectedExamBundleId = result.id;
    showBundleSelection(result.mode);
  }
}

/* ============================================================
   LOAD / BOOTSTRAP
   ============================================================ */
async function load(courseId = state.selectedCourseId) {
  try {
    state.payload = await api(`/api/questions?course_id=${encodeURIComponent(courseId || "")}`);
    state.questions = state.payload.questions;
    state.index = 0;
    state.answers = {};
    state.practiceResults = {};
    renderDashboard();
    if (!el("questionView").classList.contains("hidden")) {
      render();
    }
  } catch (error) {
    showToast("Failed to load questions: " + error.message, "bad");
    el("questionText").textContent = "Could not load questions";
    renderSkeletons();
    throw error;
  }
}

async function bootstrap() {
  try {
    const session = await api("/api/session");
    showSignedIn(session);
    await loadCourses();
  } catch (error) {
    if (error.message === "Authentication required.") {
      showSignedOut();
      return;
    }
    showToast(error.message, "bad");
    throw error;
  }
}

async function logout() {
  await api("/api/logout", { method: "POST", body: "{}" });
  clearTimer();
  state.courses = [];
  state.selectedCourseId = null;
  state.selectedCourse = null;
  state.selectedPracticeBundleId = null;
  state.selectedExamBundleId = null;
  state.bundleSelectionMode = null;
  state.sessionStartState = "idle";
  state.sessionStartError = "";
  state.startingBundleId = null;
  state.attemptId = null;
  state.studyCatalog = null;
  state.currentChapter = null;
  state.lessonIndex = 0;
  state.dashboard = null;
  state.learnerItems = [];
  state.currentResults = null;
  state.searchResults = [];
  showSignedOut();
  showToast("Signed out successfully.", "neutral");
}

/* ============================================================
   EVENT LISTENERS
   ============================================================ */
el("dashboardStudyBtn").addEventListener("click", openStudy);
el("sidebarSubjectsButton").addEventListener("click", showCourseSelection);
el("sidebarDashboardButton").addEventListener("click", goToDashboard);
el("sidebarStudyButton").addEventListener("click", openStudy);
el("sidebarExamButton").addEventListener("click", showExamPathway);
el("dashboardExamPathBtn").addEventListener("click", showExamPathway);
el("openPathwayChoiceButton").addEventListener("click", showPathwayChoice);
el("pathwayChoiceBackButton").addEventListener("click", showDashboard);
el("dashboardPracticeBtn").addEventListener("click", () => {
  showBundleSelection("practice");
});
el("dashboardExamBtn").addEventListener("click", () => {
  showBundleSelection("exam");
});
el("changeCourseButton").addEventListener("click", showCourseSelection);
el("examPathwayBackButton").addEventListener("click", showPathwayChoice);
el("bundleBackButton").addEventListener("click", showExamPathway);
el("adminButton").addEventListener("click", () => {
  openAdmin().catch((error) => showToast(error.message, "bad"));
});
el("adminBackButton").addEventListener("click", goToDashboard);
el("refreshPendingButton").addEventListener("click", () => {
  Promise.all([loadPendingIdentities(), loadApprovedIdentities(), loadAdminAudit()])
    .catch((error) => showToast(error.message, "bad"));
});
el("refreshDashboardButton").addEventListener("click", () => {
  loadDashboard().catch((error) => showToast(error.message, "bad"));
});
el("adminUserFilter").addEventListener("input", renderApprovedIdentities);
el("adminAccessFilter").addEventListener("input", renderAdminAccessRoster);
for (const button of document.querySelectorAll(".admin-section-tab")) {
  button.addEventListener("click", () => showAdminSection(button.dataset.adminSection));
}
el("previousPendingPage").addEventListener("click", () => {
  state.pendingIdentityOffset = Math.max(0, state.pendingIdentityOffset - ADMIN_PAGE_SIZE);
  loadPendingIdentities().catch((error) => showToast(error.message, "bad"));
});
el("nextPendingPage").addEventListener("click", () => {
  state.pendingIdentityOffset += ADMIN_PAGE_SIZE;
  loadPendingIdentities().catch((error) => showToast(error.message, "bad"));
});
el("previousApprovedPage").addEventListener("click", () => {
  state.approvedIdentityOffset = Math.max(0, state.approvedIdentityOffset - ADMIN_PAGE_SIZE);
  loadApprovedIdentities().catch((error) => showToast(error.message, "bad"));
});
el("nextApprovedPage").addEventListener("click", () => {
  state.approvedIdentityOffset += ADMIN_PAGE_SIZE;
  loadApprovedIdentities().catch((error) => showToast(error.message, "bad"));
});
el("previousAuditPage").addEventListener("click", () => {
  state.adminAuditOffset = Math.max(0, state.adminAuditOffset - ADMIN_PAGE_SIZE);
  loadAdminAudit().catch((error) => showToast(error.message, "bad"));
});
el("nextAuditPage").addEventListener("click", () => {
  state.adminAuditOffset += ADMIN_PAGE_SIZE;
  loadAdminAudit().catch((error) => showToast(error.message, "bad"));
});
el("studyBackButton").addEventListener("click", showDashboard);
el("chapterBackButton").addEventListener("click", openStudy);
el("continueLessonButton").addEventListener("click", () => openLesson());
el("openFlashcardsButton").addEventListener("click", openFlashcards);
el("openGroundedButton").addEventListener("click", openGroundedExplanation);
el("lessonBackButton").addEventListener("click", showChapterOverview);
el("previousLesson").addEventListener("click", () => moveLesson(-1));
el("nextLesson").addEventListener("click", () => moveLesson(1));
el("lessonFlashcardsButton").addEventListener("click", openFlashcards);
el("lessonBookmarkButton").addEventListener("click", () => {
  toggleLessonBookmark().catch((error) => showToast(error.message, "bad"));
});
el("flashcardBackButton").addEventListener("click", showChapterOverview);
el("groundedBackButton").addEventListener("click", showChapterOverview);
el("questionBackButton").addEventListener("click", goToDashboard);
el("questionMapToggle").addEventListener("click", openQuestionMap);
el("questionMapClose").addEventListener("click", () => closeQuestionMap({ restoreFocus: true }));
el("questionMapBackdrop").addEventListener("click", () => closeQuestionMap({ restoreFocus: true }));
el("completeChapterButton").addEventListener("click", () => {
  completeChapter().catch((error) => showToast(error.message, "bad"));
});
el("flashcard").addEventListener("click", () => {
  if (!state.flashcardRevealed) {
    state.flashcardRevealed = true;
    renderFlashcard();
  }
});
el("previousFlashcard").addEventListener("click", () => moveFlashcard(-1));
el("nextFlashcard").addEventListener("click", () => moveFlashcard(1));
el("flashcardAgain").addEventListener("click", () => {
  reviewFlashcard(false).catch((error) => showToast(error.message, "bad"));
});
el("flashcardKnown").addEventListener("click", () => {
  reviewFlashcard(true).catch((error) => showToast(error.message, "bad"));
});
el("groundedQuestionForm").addEventListener("submit", (event) => {
  askGroundedQuestion(event).catch((error) => showToast(error.message, "bad"));
});

el("resultsBackButton").addEventListener("click", showDashboard);
el("retakeAssessmentButton").addEventListener("click", retakeCurrentAssessment);
for (const button of document.querySelectorAll(".result-filter")) {
  button.addEventListener("click", () => {
    state.resultFilter = button.dataset.resultFilter || "all";
    renderResults();
  });
}

el("globalSearchButton").addEventListener("click", openGlobalSearch);
el("closeSearchButton").addEventListener("click", closeGlobalSearch);
el("globalSearchForm").addEventListener("submit", (event) => {
  searchCurrentCourse(event).catch((error) => showToast(error.message, "bad"));
});
el("searchOverlay").addEventListener("click", (event) => {
  if (event.target === el("searchOverlay")) closeGlobalSearch();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el("searchOverlay").classList.contains("hidden")) {
    closeGlobalSearch();
  }
  if (event.key === "Escape" && el("questionSessionPanel").classList.contains("is-open")) {
    closeQuestionMap({ restoreFocus: true });
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openGlobalSearch();
  }
});

el("lightThemeButton").addEventListener("click", () => applyTheme(LIGHT_THEME, { persist: true }));
el("darkThemeButton").addEventListener("click", () => applyTheme(DARK_THEME, { persist: true }));

el("brandLink").addEventListener("click", goToDashboard);

el("previousQuestion").addEventListener("click", () => {
  saveSelection();
  state.index = Math.max(0, state.index - 1);
  render();
});
el("nextQuestion").addEventListener("click", () => {
  saveSelection();
  state.index = Math.min(state.questions.length - 1, state.index + 1);
  render();
});

el("submitAnswer").addEventListener("click", () => {
  submitAnswer().catch((error) => showToast(error.message, "bad"));
});
el("finishExam").addEventListener("click", () => {
  finishExam().catch((error) => showToast(error.message, "bad"));
});

el("logoutButton").addEventListener("click", () => {
  logout().catch((error) => showToast(error.message, "bad"));
});

bootstrap().catch((error) => {
  el("questionText").textContent = "Could not load questions";
  showToast(error.message, "bad");
});
