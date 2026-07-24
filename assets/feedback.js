(() => {
    const form = document.getElementById("feedback-form");
    const status = document.getElementById("feedback-status");
    const submit = document.getElementById("feedback-submit");
    if (!form || !status || !submit) return;

    form.addEventListener("submit", async event => {
        event.preventDefault();
        const values = new FormData(form);
        const payload = {
            category: String(values.get("category") || "general"),
            message: String(values.get("message") || ""),
            contact: String(values.get("contact") || ""),
            company: String(values.get("company") || "")
        };
        submit.disabled = true;
        status.classList.remove("error");
        status.textContent = "Sending feedback...";
        try {
            const response = await fetch("/api/feedback", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "Feedback could not be sent.");
            form.elements.message.value = "";
            status.textContent = "Thanks. Your feedback reached the Jarvis launch queue.";
        } catch (error) {
            status.classList.add("error");
            status.textContent = error.message || "Feedback could not be sent. Please try again.";
        } finally {
            submit.disabled = false;
        }
    });
})();
