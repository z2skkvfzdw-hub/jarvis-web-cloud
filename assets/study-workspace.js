(() => {
    "use strict";

    const chatId = document.body.dataset.chatId || "";
    const profilesKey = "jarvis_subject_profiles_v1";
    const workspaceKey = `jarvis_study_workspace_${chatId}_v1`;
    const profileSelect = document.getElementById("profile-select");
    const profileName = document.getElementById("profile-name");
    const profileSubject = document.getElementById("profile-subject");
    const profileLevel = document.getElementById("profile-level");
    const profileCurriculum = document.getElementById("profile-curriculum");
    const profileGoal = document.getElementById("profile-goal");
    const topicInput = document.getElementById("study-topic");
    const notesInput = document.getElementById("study-notes");
    const targetDateInput = document.getElementById("plan-target-date");
    const planMinutesInput = document.getElementById("plan-minutes");
    const saveState = document.getElementById("study-save-state");
    const planOutput = document.getElementById("plan-output");
    const flashcardOutput = document.getElementById("flashcard-output");
    const quizOutput = document.getElementById("quiz-output");
    const planStatus = document.getElementById("plan-status");
    const flashcardStatus = document.getElementById("flashcard-status");
    const quizStatus = document.getElementById("quiz-status");
    const activityList = document.getElementById("activity-list");
    let saveTimer = null;

    function identifier() {
        if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
        return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function readJson(key, fallback) {
        try {
            const value = JSON.parse(localStorage.getItem(key) || "null");
            return value === null ? fallback : value;
        } catch (error) {
            return fallback;
        }
    }

    function emptyProgress() {
        return {
            cards_reviewed: 0,
            cards_known: 0,
            quiz_scores: [],
            minutes: 0,
            sessions: 0,
            plan_completed: 0,
            plan_total: 0,
            activity: []
        };
    }

    function cleanProgress(value) {
        const source = value && typeof value === "object" ? value : {};
        return {
            cards_reviewed: Math.max(0, Number(source.cards_reviewed) || 0),
            cards_known: Math.max(0, Number(source.cards_known) || 0),
            quiz_scores: Array.isArray(source.quiz_scores)
                ? source.quiz_scores.map(Number).filter(Number.isFinite).map(score => Math.max(0, Math.min(100, score))).slice(-20)
                : [],
            minutes: Math.max(0, Number(source.minutes) || 0),
            sessions: Math.max(0, Number(source.sessions) || 0),
            plan_completed: Math.max(0, Number(source.plan_completed) || 0),
            plan_total: Math.max(0, Number(source.plan_total) || 0),
            activity: Array.isArray(source.activity) ? source.activity.filter(Boolean).slice(0, 20) : []
        };
    }

    function cleanProfile(value) {
        const source = value && typeof value === "object" ? value : {};
        return {
            id: String(source.id || identifier()),
            name: String(source.name || "New subject").slice(0, 80),
            subject: String(source.subject || "").slice(0, 120),
            level: String(source.level || "").slice(0, 120),
            curriculum: String(source.curriculum || "").slice(0, 120),
            goal: String(source.goal || "").slice(0, 500),
            progress: cleanProgress(source.progress)
        };
    }

    function loadProfiles() {
        const saved = readJson(profilesKey, null);
        const profiles = Array.isArray(saved?.profiles) ? saved.profiles.map(cleanProfile).slice(0, 20) : [];
        if (!profiles.length) profiles.push(cleanProfile({ name: "My first subject" }));
        const activeId = profiles.some(item => item.id === saved?.active_id) ? saved.active_id : profiles[0].id;
        return { schema: 1, active_id: activeId, profiles };
    }

    function emptyWorkspace() {
        return {
            topic: "",
            notes: "",
            target_date: "",
            minutes_per_day: 30,
            active_tab: "plan",
            plan: null,
            completed_sessions: [],
            flashcards: [],
            deck: { index: 0, revealed: false, reviewed: 0, known: 0 },
            quiz: [],
            quiz_answers: {},
            quiz_submitted: false,
            quiz_score: null
        };
    }

    function cleanWorkspace(value) {
        const source = value && typeof value === "object" ? value : {};
        const activeTab = ["plan", "flashcards", "quiz"].includes(source.active_tab) ? source.active_tab : "plan";
        return {
            topic: String(source.topic || "").slice(0, 500),
            notes: String(source.notes || "").slice(0, 8000),
            target_date: String(source.target_date || "").slice(0, 32),
            minutes_per_day: Math.max(10, Math.min(240, Number(source.minutes_per_day) || 30)),
            active_tab: activeTab,
            plan: source.plan && source.plan.kind === "plan" ? source.plan : null,
            completed_sessions: Array.isArray(source.completed_sessions) ? source.completed_sessions.map(Number).filter(Number.isInteger) : [],
            flashcards: Array.isArray(source.flashcards) ? source.flashcards.slice(0, 20) : [],
            deck: {
                index: Math.max(0, Number(source.deck?.index) || 0),
                revealed: Boolean(source.deck?.revealed),
                reviewed: Math.max(0, Number(source.deck?.reviewed) || 0),
                known: Math.max(0, Number(source.deck?.known) || 0)
            },
            quiz: Array.isArray(source.quiz) ? source.quiz.slice(0, 20) : [],
            quiz_answers: source.quiz_answers && typeof source.quiz_answers === "object" ? source.quiz_answers : {},
            quiz_submitted: Boolean(source.quiz_submitted),
            quiz_score: Number.isFinite(Number(source.quiz_score)) ? Number(source.quiz_score) : null
        };
    }

    function loadWorkspaceStore() {
        const saved = readJson(workspaceKey, {});
        const values = {};
        if (saved?.profiles && typeof saved.profiles === "object") {
            Object.entries(saved.profiles).forEach(([id, value]) => { values[id] = cleanWorkspace(value); });
        }
        return { schema: 1, profiles: values };
    }

    let profileStore = loadProfiles();
    let workspaceStore = loadWorkspaceStore();

    function activeProfile() {
        return profileStore.profiles.find(item => item.id === profileStore.active_id) || profileStore.profiles[0];
    }

    function workspace() {
        const id = activeProfile().id;
        if (!workspaceStore.profiles[id]) workspaceStore.profiles[id] = emptyWorkspace();
        return workspaceStore.profiles[id];
    }

    function persist() {
        try {
            localStorage.setItem(profilesKey, JSON.stringify(profileStore));
            localStorage.setItem(workspaceKey, JSON.stringify(workspaceStore));
            saveState.textContent = "Saved on this device";
        } catch (error) {
            saveState.textContent = "Could not save";
        }
    }

    function scheduleSave() {
        saveState.textContent = "Saving...";
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(persist, 300);
    }

    function logActivity(title, detail) {
        const progress = activeProfile().progress;
        progress.activity.unshift({ title: String(title).slice(0, 100), detail: String(detail).slice(0, 180), at: new Date().toISOString() });
        progress.activity = progress.activity.slice(0, 20);
        persist();
        renderProgress();
    }

    function renderProfileOptions() {
        profileSelect.replaceChildren();
        profileStore.profiles.forEach(profile => {
            const option = document.createElement("option");
            option.value = profile.id;
            option.textContent = profile.name || profile.subject || "New subject";
            option.selected = profile.id === profileStore.active_id;
            profileSelect.append(option);
        });
    }

    function renderProfile() {
        const profile = activeProfile();
        profileName.value = profile.name;
        profileSubject.value = profile.subject;
        profileLevel.value = profile.level;
        profileCurriculum.value = profile.curriculum;
        profileGoal.value = profile.goal;
        renderProfileOptions();
    }

    function updateProfileFromFields() {
        const profile = activeProfile();
        profile.name = profileName.value.slice(0, 80) || "New subject";
        profile.subject = profileSubject.value.slice(0, 120);
        profile.level = profileLevel.value.slice(0, 120);
        profile.curriculum = profileCurriculum.value.slice(0, 120);
        profile.goal = profileGoal.value.slice(0, 500);
        renderProfileOptions();
        scheduleSave();
    }

    function renderWorkspaceInputs() {
        const value = workspace();
        topicInput.value = value.topic;
        notesInput.value = value.notes;
        targetDateInput.value = /^\d{4}-\d{2}-\d{2}$/.test(value.target_date) ? value.target_date : "";
        planMinutesInput.value = String(value.minutes_per_day);
        switchTool(value.active_tab, false);
        renderPlan();
        renderFlashcards();
        renderQuiz();
        renderProgress();
    }

    function createProfile() {
        const profile = cleanProfile({ name: `Subject ${profileStore.profiles.length + 1}` });
        profileStore.profiles.push(profile);
        profileStore.active_id = profile.id;
        workspaceStore.profiles[profile.id] = emptyWorkspace();
        persist();
        renderProfile();
        renderWorkspaceInputs();
        profileName.select();
    }

    function deleteProfile() {
        const profile = activeProfile();
        if (!window.confirm(`Delete the ${profile.name} profile and its progress?`)) return;
        workspaceStore.profiles[profile.id] = undefined;
        delete workspaceStore.profiles[profile.id];
        profileStore.profiles = profileStore.profiles.filter(item => item.id !== profile.id);
        if (!profileStore.profiles.length) profileStore.profiles.push(cleanProfile({ name: "My first subject" }));
        profileStore.active_id = profileStore.profiles[0].id;
        persist();
        renderProfile();
        renderWorkspaceInputs();
    }

    function switchTool(name, shouldSave = true) {
        if (!["plan", "flashcards", "quiz"].includes(name)) return;
        workspace().active_tab = name;
        document.querySelectorAll("[data-tool-tab]").forEach(button => {
            const active = button.dataset.toolTab === name;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
        });
        document.querySelectorAll("[data-tool-view]").forEach(view => {
            const active = view.dataset.toolView === name;
            view.classList.toggle("active", active);
            view.hidden = !active;
        });
        if (shouldSave) persist();
    }

    function generationPayload(action, count) {
        const profile = activeProfile();
        const value = workspace();
        return {
            action,
            subject: profile.subject.trim(),
            level: profile.level.trim(),
            curriculum: profile.curriculum.trim(),
            goal: profile.goal.trim(),
            topic: value.topic.trim(),
            notes: value.notes.trim(),
            target_date: value.target_date,
            minutes_per_day: value.minutes_per_day,
            count
        };
    }

    function generationGuard(status) {
        if (!activeProfile().subject.trim()) {
            status.textContent = "Add the subject to this profile first.";
            profileSubject.focus();
            return false;
        }
        if (!workspace().topic.trim()) {
            status.textContent = "Add the topic you want to study.";
            topicInput.focus();
            return false;
        }
        return true;
    }

    async function generateTool(action) {
        const settings = {
            plan: { button: document.getElementById("generate-plan"), status: planStatus, count: 8 },
            flashcards: { button: document.getElementById("generate-flashcards"), status: flashcardStatus, count: Number(document.getElementById("flashcard-count").value) || 8 },
            quiz: { button: document.getElementById("generate-quiz"), status: quizStatus, count: Number(document.getElementById("quiz-count").value) || 8 }
        }[action];
        if (!settings || !generationGuard(settings.status)) return;
        settings.button.disabled = true;
        settings.status.textContent = action === "plan" ? "Building your study sequence..." : action === "flashcards" ? "Creating retrieval cards..." : "Writing your quiz...";
        try {
            const response = await fetch(`/api/chats/${chatId}/study-tools`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(generationPayload(action, settings.count))
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Jarvis could not generate that study material.");
            const value = workspace();
            if (action === "plan") {
                value.plan = payload.result;
                value.completed_sessions = [];
                activeProfile().progress.plan_completed = 0;
                activeProfile().progress.plan_total = payload.result.sessions.length;
                renderPlan();
            } else if (action === "flashcards") {
                value.flashcards = payload.result.cards;
                value.deck = { index: 0, revealed: false, reviewed: 0, known: 0 };
                renderFlashcards();
            } else {
                value.quiz = payload.result.questions;
                value.quiz_answers = {};
                value.quiz_submitted = false;
                value.quiz_score = null;
                renderQuiz();
            }
            settings.status.textContent = `Ready in ${(Number(payload.elapsed_ms || 0) / 1000).toFixed(1)}s.`;
            persist();
            renderProgress();
        } catch (error) {
            settings.status.textContent = error.message || "Generation failed. Try again.";
        } finally {
            settings.button.disabled = false;
        }
    }

    function renderPlan() {
        const value = workspace();
        planOutput.replaceChildren();
        if (!value.plan?.sessions?.length) return;
        const header = document.createElement("div");
        header.className = "plan-header";
        const title = document.createElement("h2");
        title.textContent = value.plan.title || "Study plan";
        const summary = document.createElement("p");
        summary.textContent = value.plan.summary || "";
        header.append(title, summary);
        const sessions = document.createElement("div");
        sessions.className = "plan-sessions";
        value.plan.sessions.forEach((session, index) => {
            const row = document.createElement("div");
            row.className = "plan-session";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = value.completed_sessions.includes(index);
            checkbox.setAttribute("aria-label", `Complete ${session.title}`);
            checkbox.addEventListener("change", () => togglePlanSession(index, session));
            const copy = document.createElement("div");
            copy.className = "session-copy";
            const heading = document.createElement("h3");
            heading.textContent = session.title;
            const objective = document.createElement("p");
            objective.textContent = session.objective;
            copy.append(heading, objective);
            if (Array.isArray(session.activities) && session.activities.length) {
                const list = document.createElement("ul");
                session.activities.forEach(activity => {
                    const item = document.createElement("li");
                    item.textContent = activity;
                    list.append(item);
                });
                copy.append(list);
            }
            const minutes = document.createElement("span");
            minutes.className = "session-minutes";
            minutes.textContent = `${session.minutes} min`;
            row.append(checkbox, copy, minutes);
            sessions.append(row);
        });
        planOutput.append(header, sessions);
    }

    function togglePlanSession(index, session) {
        const value = workspace();
        const progress = activeProfile().progress;
        const completed = value.completed_sessions.includes(index);
        if (completed) {
            value.completed_sessions = value.completed_sessions.filter(item => item !== index);
            progress.minutes = Math.max(0, progress.minutes - Number(session.minutes || 0));
            progress.sessions = Math.max(0, progress.sessions - 1);
        } else {
            value.completed_sessions.push(index);
            value.completed_sessions.sort((a, b) => a - b);
            progress.minutes += Number(session.minutes || 0);
            progress.sessions += 1;
            logActivity("Study session completed", `${session.title} / ${session.minutes} minutes`);
        }
        progress.plan_completed = value.completed_sessions.length;
        progress.plan_total = value.plan.sessions.length;
        persist();
        renderPlan();
        renderProgress();
    }

    function renderFlashcards() {
        const value = workspace();
        flashcardOutput.replaceChildren();
        if (!value.flashcards.length) return;
        if (value.deck.index >= value.flashcards.length) {
            const finished = document.createElement("div");
            finished.className = "deck-finished";
            const title = document.createElement("strong");
            title.textContent = "Deck complete";
            const result = document.createElement("span");
            result.textContent = `${value.deck.known} known from ${value.deck.reviewed} reviewed`;
            const restart = document.createElement("button");
            restart.type = "button";
            restart.textContent = "Review again";
            restart.addEventListener("click", () => {
                value.deck = { index: 0, revealed: false, reviewed: 0, known: 0 };
                persist();
                renderFlashcards();
            });
            finished.append(title, result, restart);
            flashcardOutput.append(finished);
            return;
        }
        const card = value.flashcards[value.deck.index];
        const progress = document.createElement("div");
        progress.className = "deck-progress";
        progress.append(document.createTextNode(`Card ${value.deck.index + 1} of ${value.flashcards.length}`));
        const known = document.createElement("span");
        known.textContent = `${value.deck.known} known`;
        progress.append(known);
        const face = document.createElement("button");
        face.type = "button";
        face.className = "flashcard";
        face.setAttribute("aria-label", value.deck.revealed ? "Flashcard answer" : "Reveal flashcard answer");
        const content = document.createElement("div");
        const label = document.createElement("span");
        label.className = "card-label";
        label.textContent = value.deck.revealed ? "Answer" : "Question";
        const main = document.createElement("strong");
        main.textContent = value.deck.revealed ? card.back : card.front;
        content.append(label, main);
        if (!value.deck.revealed && card.hint) {
            const hint = document.createElement("p");
            hint.textContent = `Hint: ${card.hint}`;
            content.append(hint);
        }
        face.append(content);
        face.addEventListener("click", () => {
            value.deck.revealed = true;
            persist();
            renderFlashcards();
        });
        const actions = document.createElement("div");
        actions.className = "deck-actions";
        if (!value.deck.revealed) {
            const reveal = document.createElement("button");
            reveal.type = "button";
            reveal.textContent = "Reveal answer";
            reveal.addEventListener("click", () => {
                value.deck.revealed = true;
                persist();
                renderFlashcards();
            });
            actions.style.gridTemplateColumns = "1fr";
            actions.append(reveal);
        } else {
            [["Again", "again"], ["Hard", "hard"], ["Know it", "known"]].forEach(([labelText, rating]) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = `rate-${rating}`;
                button.textContent = labelText;
                button.addEventListener("click", () => rateCard(rating));
                actions.append(button);
            });
        }
        flashcardOutput.append(progress, face, actions);
    }

    function rateCard(rating) {
        const value = workspace();
        const progress = activeProfile().progress;
        value.deck.reviewed += 1;
        progress.cards_reviewed += 1;
        if (rating === "known") {
            value.deck.known += 1;
            progress.cards_known += 1;
        }
        value.deck.index += 1;
        value.deck.revealed = false;
        if (value.deck.index >= value.flashcards.length) {
            progress.sessions += 1;
            logActivity("Flashcard deck completed", `${value.deck.known} known from ${value.deck.reviewed}`);
        }
        persist();
        renderFlashcards();
        renderProgress();
    }

    function renderQuiz() {
        const value = workspace();
        quizOutput.replaceChildren();
        if (!value.quiz.length) return;
        value.quiz.forEach((question, questionIndex) => {
            const section = document.createElement("section");
            section.className = "quiz-question";
            const heading = document.createElement("h3");
            heading.textContent = `${questionIndex + 1}. ${question.prompt}`;
            const options = document.createElement("div");
            options.className = "quiz-options";
            question.options.forEach((option, optionIndex) => {
                const label = document.createElement("label");
                label.className = "quiz-option";
                if (value.quiz_submitted) {
                    if (optionIndex === question.answer_index) label.classList.add("correct");
                    else if (Number(value.quiz_answers[questionIndex]) === optionIndex) label.classList.add("incorrect");
                }
                const input = document.createElement("input");
                input.type = "radio";
                input.name = `question-${questionIndex}`;
                input.value = String(optionIndex);
                input.checked = Number(value.quiz_answers[questionIndex]) === optionIndex;
                input.disabled = value.quiz_submitted;
                input.addEventListener("change", () => {
                    value.quiz_answers[questionIndex] = optionIndex;
                    persist();
                });
                const text = document.createElement("span");
                text.textContent = option;
                label.append(input, text);
                options.append(label);
            });
            section.append(heading, options);
            if (value.quiz_submitted) {
                const explanation = document.createElement("p");
                explanation.className = "question-explanation";
                explanation.textContent = question.explanation;
                section.append(explanation);
            }
            quizOutput.append(section);
        });
        if (!value.quiz_submitted) {
            const submit = document.createElement("button");
            submit.type = "submit";
            submit.className = "primary quiz-submit";
            submit.textContent = "Check answers";
            quizOutput.append(submit);
        } else {
            const score = document.createElement("div");
            score.className = "quiz-score";
            score.textContent = `Score: ${value.quiz_score}%`;
            quizOutput.append(score);
        }
    }

    function submitQuiz(event) {
        event.preventDefault();
        const value = workspace();
        if (!value.quiz.length || value.quiz_submitted) return;
        const answered = Object.keys(value.quiz_answers).length;
        if (answered < value.quiz.length) {
            quizStatus.textContent = `Answer all ${value.quiz.length} questions before checking.`;
            return;
        }
        const correct = value.quiz.reduce((total, question, index) => total + (Number(value.quiz_answers[index]) === question.answer_index ? 1 : 0), 0);
        const score = Math.round((correct / value.quiz.length) * 100);
        value.quiz_submitted = true;
        value.quiz_score = score;
        const progress = activeProfile().progress;
        progress.quiz_scores.push(score);
        progress.quiz_scores = progress.quiz_scores.slice(-20);
        progress.sessions += 1;
        progress.minutes += Math.max(1, Math.round(value.quiz.length * 1.5));
        quizStatus.textContent = `${correct} of ${value.quiz.length} correct.`;
        logActivity("Quiz completed", `${score}% on ${value.topic || activeProfile().subject}`);
        persist();
        renderQuiz();
        renderProgress();
    }

    function renderProgress() {
        const progress = activeProfile().progress;
        const planPercent = progress.plan_total ? Math.round((progress.plan_completed / progress.plan_total) * 100) : 0;
        const quizAverage = progress.quiz_scores.length
            ? Math.round(progress.quiz_scores.reduce((sum, score) => sum + score, 0) / progress.quiz_scores.length)
            : null;
        document.getElementById("plan-progress").textContent = `${planPercent}%`;
        document.getElementById("cards-reviewed").textContent = String(progress.cards_reviewed);
        document.getElementById("cards-known").textContent = String(progress.cards_known);
        document.getElementById("quiz-average").textContent = quizAverage === null ? "--" : `${quizAverage}%`;
        document.getElementById("study-minutes").textContent = `${Math.round(progress.minutes)}m`;
        document.getElementById("study-sessions").textContent = String(progress.sessions);
        activityList.replaceChildren();
        if (!progress.activity.length) {
            const empty = document.createElement("div");
            empty.className = "activity-empty";
            empty.textContent = "No study activity yet.";
            activityList.append(empty);
            return;
        }
        progress.activity.forEach(entry => {
            const item = document.createElement("div");
            item.className = "activity-item";
            const title = document.createElement("strong");
            title.textContent = String(entry.title || "Study activity");
            const detail = document.createElement("span");
            const date = new Date(entry.at || "");
            detail.textContent = `${String(entry.detail || "")}${Number.isFinite(date.getTime()) ? ` / ${date.toLocaleString()}` : ""}`;
            item.append(title, detail);
            activityList.append(item);
        });
    }

    function resetProgress() {
        if (!window.confirm(`Reset all progress for ${activeProfile().name}?`)) return;
        activeProfile().progress = emptyProgress();
        workspace().completed_sessions = [];
        persist();
        renderPlan();
        renderProgress();
    }

    renderProfile();
    renderWorkspaceInputs();

    [profileName, profileSubject, profileLevel, profileCurriculum, profileGoal].forEach(input => input.addEventListener("input", updateProfileFromFields));
    profileSelect.addEventListener("change", () => {
        profileStore.active_id = profileSelect.value;
        persist();
        renderProfile();
        renderWorkspaceInputs();
    });
    document.getElementById("new-profile").addEventListener("click", createProfile);
    document.getElementById("delete-profile").addEventListener("click", deleteProfile);
    topicInput.addEventListener("input", () => { workspace().topic = topicInput.value; scheduleSave(); });
    notesInput.addEventListener("input", () => { workspace().notes = notesInput.value; scheduleSave(); });
    targetDateInput.addEventListener("change", () => { workspace().target_date = targetDateInput.value; persist(); });
    planMinutesInput.addEventListener("change", () => {
        workspace().minutes_per_day = Math.max(10, Math.min(240, Number(planMinutesInput.value) || 30));
        planMinutesInput.value = String(workspace().minutes_per_day);
        persist();
    });
    document.querySelectorAll("[data-tool-tab]").forEach(button => button.addEventListener("click", () => switchTool(button.dataset.toolTab)));
    document.getElementById("generate-plan").addEventListener("click", () => generateTool("plan"));
    document.getElementById("generate-flashcards").addEventListener("click", () => generateTool("flashcards"));
    document.getElementById("generate-quiz").addEventListener("click", () => generateTool("quiz"));
    quizOutput.addEventListener("submit", submitQuiz);
    document.getElementById("reset-progress").addEventListener("click", resetProgress);
    window.addEventListener("beforeunload", persist);
})();
