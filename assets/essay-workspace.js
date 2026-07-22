(() => {
    "use strict";

    const chatId = document.body.dataset.chatId || "";
    const storageKey = `jarvis_essay_workspace_${chatId}_v1`;
    const assignmentMemoryKey = `jarvis_assignment_memory_${chatId}_v1`;
    const titleInput = document.getElementById("essay-title");
    const assignmentInput = document.getElementById("assignment-brief");
    const draftEditor = document.getElementById("draft-editor");
    const saveState = document.getElementById("save-state");
    const sourceStatus = document.getElementById("source-status");
    const wordCount = document.getElementById("word-count");
    const characterCount = document.getElementById("character-count");
    const versionList = document.getElementById("version-list");
    const reviewStatus = document.getElementById("review-status");
    const reviewResult = document.getElementById("review-result");
    const selfCheckButton = document.getElementById("run-self-check");
    const maxDraftCharacters = Number(draftEditor.maxLength) || 24000;
    let saveTimer = null;

    function emptyState() {
        return {
            schema: 1,
            title: "",
            assignment: "",
            rubric: null,
            feedback: null,
            draft: "",
            versions: [],
            review: null,
            updated_at: ""
        };
    }

    function cleanDocument(value) {
        if (!value || typeof value !== "object") return null;
        const name = String(value.name || "document.txt").slice(0, 160);
        const mediaType = String(value.media_type || "text/plain").slice(0, 120);
        const text = String(value.text || "").slice(0, 24000);
        if (!text.trim()) return null;
        return { name, media_type: mediaType, text, characters: text.length };
    }

    function loadState() {
        try {
            const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
            if (!saved || typeof saved !== "object") return emptyState();
            return {
                schema: 1,
                title: String(saved.title || "").slice(0, 160),
                assignment: String(saved.assignment || "").slice(0, Number(assignmentInput.maxLength) || 8000),
                rubric: cleanDocument(saved.rubric),
                feedback: cleanDocument(saved.feedback),
                draft: String(saved.draft || "").slice(0, maxDraftCharacters),
                versions: Array.isArray(saved.versions)
                    ? saved.versions.filter(item => item && item.id && typeof item.draft === "string").slice(0, 30)
                    : [],
                review: saved.review && typeof saved.review === "object" ? saved.review : null,
                updated_at: String(saved.updated_at || "")
            };
        } catch (error) {
            return emptyState();
        }
    }

    let state = loadState();

    function readJson(key, fallback) {
        try {
            const value = JSON.parse(localStorage.getItem(key) || "null");
            return value === null ? fallback : value;
        } catch (error) {
            return fallback;
        }
    }

    function syncAssignmentMemory() {
        const current = readJson(assignmentMemoryKey, []);
        const preserved = Array.isArray(current)
            ? current.filter(item => {
                const name = String(item?.name || "");
                return !name.startsWith("Essay rubric - ") && !name.startsWith("Teacher feedback - ");
            })
            : [];
        if (state.rubric) {
            preserved.push({
                name: `Essay rubric - ${state.rubric.name}`,
                media_type: state.rubric.media_type,
                text: state.rubric.text,
                saved_at: new Date().toISOString()
            });
        }
        if (state.feedback) {
            preserved.push({
                name: `Teacher feedback - ${state.feedback.name}`,
                media_type: state.feedback.media_type,
                text: state.feedback.text,
                saved_at: new Date().toISOString()
            });
        }
        try {
            localStorage.setItem(assignmentMemoryKey, JSON.stringify(preserved.slice(-3)));
        } catch (error) {
            sourceStatus.textContent = "The source was read, but this browser could not save it.";
        }
    }

    function persistState() {
        state.updated_at = new Date().toISOString();
        try {
            localStorage.setItem(storageKey, JSON.stringify(state));
            saveState.textContent = "Saved on this device";
        } catch (error) {
            saveState.textContent = "Could not save";
        }
    }

    function scheduleSave() {
        saveState.textContent = "Saving...";
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(persistState, 350);
    }

    function updateCounts() {
        const text = state.draft.trim();
        const words = text ? text.split(/\s+/).filter(Boolean).length : 0;
        wordCount.textContent = `${words} ${words === 1 ? "word" : "words"}`;
        characterCount.textContent = `${state.draft.length} / ${maxDraftCharacters}`;
    }

    function renderSource(type) {
        const documentValue = state[type];
        const upload = document.querySelector(`[data-upload="${type}"]`);
        const summary = document.getElementById(`${type}-summary`);
        const remove = document.querySelector(`[data-remove-source="${type}"]`);
        upload.hidden = Boolean(documentValue);
        summary.hidden = !documentValue;
        remove.hidden = !documentValue;
        summary.replaceChildren();
        if (!documentValue) return;
        const name = document.createElement("strong");
        name.textContent = documentValue.name;
        const details = document.createElement("small");
        details.textContent = `${Number(documentValue.characters || documentValue.text.length).toLocaleString()} characters`;
        summary.append(name, details);
    }

    function versionLabel(index) {
        return `Version ${state.versions.length - index}`;
    }

    function renderVersions() {
        versionList.replaceChildren();
        if (!state.versions.length) {
            const empty = document.createElement("div");
            empty.className = "version-empty";
            empty.textContent = "No saved versions yet.";
            versionList.append(empty);
            return;
        }
        state.versions.forEach((version, index) => {
            const row = document.createElement("div");
            row.className = "version-row";
            const copy = document.createElement("div");
            copy.className = "version-copy";
            const name = document.createElement("strong");
            name.textContent = String(version.label || versionLabel(index));
            const meta = document.createElement("span");
            const words = String(version.draft || "").trim().split(/\s+/).filter(Boolean).length;
            const date = new Date(version.created_at || Number(version.id));
            meta.textContent = `${Number.isFinite(date.getTime()) ? date.toLocaleString() : "Saved version"} / ${words} words`;
            copy.append(name, meta);

            const actions = document.createElement("div");
            actions.className = "version-actions";
            const restore = document.createElement("button");
            restore.type = "button";
            restore.textContent = "Restore";
            restore.addEventListener("click", () => restoreVersion(version));
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "delete-version";
            remove.setAttribute("aria-label", `Delete ${name.textContent}`);
            remove.title = `Delete ${name.textContent}`;
            remove.textContent = "Delete";
            remove.addEventListener("click", () => deleteVersion(version.id));
            actions.append(restore, remove);
            row.append(copy, actions);
            versionList.append(row);
        });
    }

    function renderReview() {
        const review = state.review;
        reviewResult.hidden = !review?.answer;
        reviewResult.textContent = review?.answer || "";
        if (!review?.answer) {
            reviewStatus.textContent = "";
            return;
        }
        const changed = String(review.draft || "") !== state.draft;
        reviewStatus.textContent = changed
            ? "Draft changed since this check. Run it again when ready."
            : `Checked ${review.checked_at || "recently"}`;
    }

    function saveVersion() {
        if (!state.draft.trim()) {
            saveState.textContent = "Write something before saving a version";
            draftEditor.focus();
            return;
        }
        const label = `Version ${state.versions.length + 1}`;
        state.versions.unshift({
            id: String(Date.now()),
            label,
            created_at: new Date().toISOString(),
            draft: state.draft
        });
        state.versions = state.versions.slice(0, 30);
        persistState();
        renderVersions();
    }

    function restoreVersion(version) {
        if (!window.confirm(`Restore ${version.label || "this version"}? Your current draft will be replaced.`)) return;
        state.draft = String(version.draft || "").slice(0, maxDraftCharacters);
        draftEditor.value = state.draft;
        persistState();
        updateCounts();
        renderReview();
        draftEditor.focus();
    }

    function deleteVersion(versionId) {
        const version = state.versions.find(item => item.id === versionId);
        if (!version || !window.confirm(`Delete ${version.label || "this version"}?`)) return;
        state.versions = state.versions.filter(item => item.id !== versionId);
        persistState();
        renderVersions();
    }

    async function uploadSource(type, file) {
        if (!file) return;
        const uploadButton = document.querySelector(`[data-upload="${type}"]`);
        sourceStatus.textContent = `Reading ${file.name}...`;
        uploadButton.disabled = true;
        try {
            const formData = new FormData();
            formData.append("file", file);
            const response = await fetch(`/api/chats/${chatId}/attachments`, { method: "POST", body: formData });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Jarvis could not read that file.");
            state[type] = cleanDocument(payload);
            if (!state[type]) throw new Error("That document did not contain readable text.");
            persistState();
            syncAssignmentMemory();
            renderSource(type);
            sourceStatus.textContent = `${file.name} added.`;
        } catch (error) {
            sourceStatus.textContent = error.message || "That document could not be added.";
        } finally {
            uploadButton.disabled = false;
            const input = document.getElementById(`${type}-file`);
            if (input) input.value = "";
        }
    }

    function removeSource(type) {
        if (!state[type] || !window.confirm(`Remove ${type === "rubric" ? "the rubric" : "teacher feedback"} from this workspace?`)) return;
        state[type] = null;
        persistState();
        syncAssignmentMemory();
        renderSource(type);
        sourceStatus.textContent = `${type === "rubric" ? "Rubric" : "Teacher feedback"} removed.`;
    }

    async function runSelfCheck() {
        if (!state.draft.trim()) {
            reviewStatus.textContent = "Add a draft before running the check.";
            draftEditor.focus();
            return;
        }
        if (!state.rubric) {
            reviewStatus.textContent = "Add the rubric before running the check.";
            document.querySelector('[data-upload="rubric"]')?.focus();
            return;
        }
        persistState();
        selfCheckButton.disabled = true;
        reviewStatus.textContent = "Checking the draft against each criterion...";
        reviewResult.hidden = true;
        try {
            const response = await fetch(`/api/chats/${chatId}/essay-check`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    title: state.title,
                    assignment: state.assignment,
                    draft: state.draft,
                    rubric: state.rubric,
                    feedback: state.feedback
                })
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Jarvis could not complete the rubric check.");
            state.review = {
                answer: String(payload.answer || "No review was returned."),
                checked_at: String(payload.checked_at || new Date().toLocaleString()),
                draft: state.draft
            };
            persistState();
            renderReview();
        } catch (error) {
            reviewStatus.textContent = error.message || "The rubric check failed. Try again.";
        } finally {
            selfCheckButton.disabled = false;
        }
    }

    titleInput.value = state.title;
    assignmentInput.value = state.assignment;
    draftEditor.value = state.draft;
    updateCounts();
    renderSource("rubric");
    renderSource("feedback");
    renderVersions();
    renderReview();

    titleInput.addEventListener("input", () => {
        state.title = titleInput.value;
        scheduleSave();
    });
    assignmentInput.addEventListener("input", () => {
        state.assignment = assignmentInput.value;
        scheduleSave();
    });
    draftEditor.addEventListener("input", () => {
        state.draft = draftEditor.value;
        updateCounts();
        renderReview();
        scheduleSave();
    });

    document.querySelectorAll("[data-upload]").forEach(button => {
        const type = button.dataset.upload;
        const input = document.getElementById(`${type}-file`);
        button.addEventListener("click", () => input?.click());
        input?.addEventListener("change", () => uploadSource(type, input.files?.[0]));
        ["dragenter", "dragover"].forEach(eventName => button.addEventListener(eventName, event => {
            event.preventDefault();
            button.classList.add("dragging");
        }));
        ["dragleave", "drop"].forEach(eventName => button.addEventListener(eventName, event => {
            event.preventDefault();
            button.classList.remove("dragging");
        }));
        button.addEventListener("drop", event => uploadSource(type, event.dataTransfer?.files?.[0]));
    });

    document.querySelectorAll("[data-remove-source]").forEach(button => {
        button.addEventListener("click", () => removeSource(button.dataset.removeSource));
    });
    document.getElementById("save-version")?.addEventListener("click", saveVersion);
    document.getElementById("snapshot-draft")?.addEventListener("click", saveVersion);
    selfCheckButton.addEventListener("click", runSelfCheck);
    window.addEventListener("beforeunload", persistState);
})();
