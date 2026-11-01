// EventSource with a watchdog. Browsers retry on their own for dropped connections but give up on
// HTTP errors, and a stalled proxy can leave a socket open and silent, so both are handled here.

const EVENTS = ["heartbeat", "run", "alert", "dispatch", "scene"];
const SILENCE_LIMIT_MS = 40000; // heartbeat is every 15 s

export function connectEvents({ onEvent, onState, url = "/api/events/stream" }) {
  let source = null;
  let retryTimer = null;
  let watchdog = null;
  let attempts = 0;
  let lastEventAt = 0;
  let stopped = false;

  function setState(state) {
    onState(state);
  }

  function scheduleRetry() {
    if (stopped) return;
    closeSource();
    setState("reconnecting");
    const delay = Math.min(15000, 1000 * 2 ** Math.min(attempts, 4));
    attempts += 1;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(open, delay);
  }

  function closeSource() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function open() {
    if (stopped) return;
    closeSource();
    let opened;
    try {
      opened = new EventSource(url);
    } catch {
      scheduleRetry();
      return;
    }
    source = opened;
    lastEventAt = Date.now();
    opened.onopen = () => {
      const wasRetrying = attempts > 0;
      attempts = 0;
      lastEventAt = Date.now();
      setState("live");
      if (wasRetrying) onEvent("reconnected", null);
    };
    opened.onerror = () => {
      if (opened !== source) return;
      if (opened.readyState === EventSource.CLOSED) {
        scheduleRetry();
      } else {
        attempts = Math.max(attempts, 1);
        setState("reconnecting");
      }
    };
    for (const name of EVENTS) {
      opened.addEventListener(name, (event) => {
        lastEventAt = Date.now();
        let data = null;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        onEvent(name, data);
      });
    }
  }

  watchdog = setInterval(() => {
    if (source && Date.now() - lastEventAt > SILENCE_LIMIT_MS) scheduleRetry();
  }, 5000);

  open();

  return {
    close() {
      stopped = true;
      clearTimeout(retryTimer);
      clearInterval(watchdog);
      closeSource();
    },
  };
}
