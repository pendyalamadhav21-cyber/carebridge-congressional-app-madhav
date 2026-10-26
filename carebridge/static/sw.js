"use strict";

// Shows care-plan reminders pushed by the server, even when no CareBridge tab is open.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "CareBridge", {
      body: data.body || "You have a care plan reminder.",
      tag: data.tag,
      renotify: true,
      requireInteraction: true,
      icon: "/static/icon.svg",
      badge: "/static/icon.svg",
      data: { url: data.url || "/patient" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/patient", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const open = wins.find((w) => w.url.startsWith(self.location.origin));
      if (open) {
        open.navigate(url);
        return open.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
