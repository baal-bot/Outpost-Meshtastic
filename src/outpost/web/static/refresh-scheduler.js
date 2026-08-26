export class RefreshScheduler {
  constructor({documentRef = document, random = Math.random} = {}) {
    this.document = documentRef;
    this.random = random;
    this.tasks = new Map();
    this.document.addEventListener("visibilitychange", () => this.visibilityChanged());
  }

  schedule(name, callback, {interval, initial = interval, essential = false} = {}) {
    if (
      !name || typeof callback !== "function" ||
      !Number.isFinite(interval) || !(interval > 0) ||
      !Number.isFinite(initial) || initial < 0
    ) {
      throw new TypeError("refresh task requires a name, callback, and positive interval");
    }
    this.cancel(name);
    const task = {
      name, callback, interval, initial, essential,
      timer: null, running: false, failures: 0, runs: 0, overlapSkips: 0,
    };
    this.tasks.set(name, task);
    this.arm(task, initial);
    return () => this.cancel(name);
  }

  cancel(name) {
    const task = this.tasks.get(name);
    if (!task) return;
    if (task.timer !== null) clearTimeout(task.timer);
    this.tasks.delete(name);
  }

  arm(task, delay) {
    if (this.tasks.get(task.name) !== task || (this.document.hidden && !task.essential)) return;
    if (task.timer !== null) clearTimeout(task.timer);
    const jitter = Math.min(1_000, Math.max(0, delay * 0.1)) * this.random();
    task.timer = setTimeout(() => this.invoke(task), delay + jitter);
  }

  async invoke(task) {
    task.timer = null;
    if (this.tasks.get(task.name) !== task) return;
    if (this.document.hidden && !task.essential) return;
    if (task.running) {
      task.overlapSkips += 1;
      this.arm(task, task.interval);
      return;
    }
    task.running = true;
    try {
      await task.callback();
      task.failures = 0;
      task.runs += 1;
    } catch (error) {
      task.failures = Math.min(task.failures + 1, 3);
      console.warn(`Refresh task ${task.name} failed; retrying with backoff.`, error);
    } finally {
      task.running = false;
      this.arm(task, task.interval * (2 ** task.failures));
    }
  }

  visibilityChanged() {
    for (const task of this.tasks.values()) {
      if (this.document.hidden && !task.essential) {
        if (task.timer !== null) clearTimeout(task.timer);
        task.timer = null;
      } else if (!this.document.hidden && task.timer === null && !task.running) {
        this.arm(task, Math.min(1_000, task.interval * 0.1));
      }
    }
  }

  snapshot() {
    return [...this.tasks.values()].map(({name, running, failures, runs, overlapSkips}) => ({
      name, running, failures, runs, overlapSkips,
    }));
  }
}

export const scheduler = new RefreshScheduler();
window.OutpostScheduler = scheduler;
