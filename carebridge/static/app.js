"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";

// Daily checklist: toggle an item done / not done.
document.querySelectorAll("[data-toggle]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const res = await fetch(btn.dataset.toggle, { method: "POST", headers: { "X-CSRF-Token": CSRF } });
      if (!res.ok) throw new Error();
      const { done } = await res.json();
      const li = btn.closest(".check-item");
      li.classList.toggle("done", done);
      btn.setAttribute("aria-pressed", String(done));
      const total = document.querySelectorAll(".check-item").length;
      const doneCount = document.querySelectorAll(".check-item.done").length;
      document.querySelector("[data-done]").textContent = doneCount;
      document.querySelector("[data-ring-fg]")?.setAttribute("stroke-dasharray", `${Math.round((100 * doneCount) / total)} 100`);
    } catch {
      alert("Couldn't save that. Check your connection and try again.");
    } finally {
      btn.disabled = false;
    }
  });
});

// Reminder notifications: register the service worker and manage this device's push subscription.
const reminderBox = document.querySelector("[data-reminders]");
if (reminderBox) {
  const status = reminderBox.querySelector("[data-reminder-status]");
  const onBtn = reminderBox.querySelector("[data-reminder-on]");
  const offBtn = reminderBox.querySelector("[data-reminder-off]");
  const testBtn = reminderBox.querySelector("[data-reminder-test]");
  const post = (url, body) =>
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(body || {}) });
  const keyBytes = (b64) => {
    const raw = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };
  const show = (subscribed) => {
    onBtn.hidden = subscribed;
    offBtn.hidden = testBtn.hidden = !subscribed;
    status.textContent = subscribed
      ? "Reminders are on for this device."
      : "Reminders are off for this device. Turn them on to be notified when items are due.";
  };

  (async () => {
    const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      status.textContent = /iPhone|iPad/.test(navigator.userAgent) && !standalone
        ? "On iPhone and iPad, tap Share, then Add to Home Screen, and open CareBridge from there to turn on reminders."
        : "This browser does not support notifications.";
      return;
    }
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    if (Notification.permission === "denied") {
      status.textContent = "Notifications are blocked for this site. Allow them in your browser settings to get reminders.";
      return;
    }
    let sub = await reg.pushManager.getSubscription();
    if (sub) await post(reminderBox.dataset.subscribe, sub.toJSON()); // keep the server copy fresh
    show(Boolean(sub));

    onBtn.addEventListener("click", async () => {
      onBtn.disabled = true;
      try {
        if ((await Notification.requestPermission()) !== "granted") {
          status.textContent = "Permission was not granted, so reminders stay off.";
          return;
        }
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(reminderBox.dataset.vapid) });
        const res = await post(reminderBox.dataset.subscribe, sub.toJSON());
        if (!res.ok) throw new Error();
        show(true);
      } catch {
        status.textContent = "Couldn't turn on reminders. Try again.";
      } finally {
        onBtn.disabled = false;
      }
    });

    offBtn.addEventListener("click", async () => {
      if (sub) {
        await post(reminderBox.dataset.unsubscribe, { endpoint: sub.endpoint });
        await sub.unsubscribe();
        sub = null;
      }
      show(false);
    });

    testBtn.addEventListener("click", async () => {
      const res = await post(reminderBox.dataset.test);
      const { sent } = await res.json();
      status.textContent = sent ? "Test notification sent." : "The test could not be delivered. Try turning reminders off and on again.";
    });
  })().catch(() => {
    status.textContent = window.isSecureContext
      ? "Reminders couldn't be set up in this browser. Try Chrome, Edge, Firefox or Safari."
      : "Reminders need a secure (HTTPS) connection.";
  });
}

// Photo upload preview.
const previewInput = document.querySelector("[data-preview-input]");
if (previewInput) {
  previewInput.addEventListener("change", () => {
    const img = document.querySelector("[data-preview]");
    const file = previewInput.files[0];
    if (!file) return;
    img.src = URL.createObjectURL(file);
    img.hidden = false;
    document.querySelector(".drop-text").textContent = file.name;
  });
}

// Forms that take a while (AI calls): show progress and prevent double submits.
document.querySelectorAll("[data-busy-form]").forEach((form) => {
  form.addEventListener("submit", () => {
    const btn = form.querySelector("[data-busy-label]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = btn.dataset.busyLabel;
    }
  });
});

// Companion chat with streamed replies.
const chatForm = document.querySelector("[data-chat-form]");
if (chatForm) {
  const log = document.querySelector("[data-chat-log]");
  const input = chatForm.querySelector("[data-chat-input]");
  const sendBtn = chatForm.querySelector("button");
  const crisisTpl = document.querySelector("[data-crisis-template]");
  let busy = false;
  const scroll = () => window.scrollTo(0, document.body.scrollHeight);

  const bubble = (cls, text) => {
    const div = document.createElement("div");
    div.className = "bubble " + cls;
    div.textContent = text;
    log.appendChild(div);
    scroll();
    return div;
  };

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    document.querySelector("[data-chips]")?.remove();
    bubble("user", text);
    input.value = "";
    const reply = bubble("assistant typing", "");
    try {
      const res = await fetch(chatForm.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify({ message: text }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "Request failed");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const event = chunk.match(/^event: (.*)$/m)?.[1];
          const data = JSON.parse(chunk.match(/^data: (.*)$/m)?.[1] ?? "null");
          if (event === "crisis") {
            log.insertBefore(crisisTpl.content.firstElementChild.cloneNode(true), reply);
          } else if (event === "delta") {
            reply.textContent += data;
            scroll();
          } else if (event === "error") {
            bubble("error", data);
          }
        }
      }
    } catch (err) {
      bubble("error", "Something went wrong: " + err.message);
    }
    reply.classList.remove("typing");
    if (!reply.textContent) reply.remove();
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => send(c.textContent)));
  scroll();
}
