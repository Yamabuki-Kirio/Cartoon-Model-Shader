/** 极小的订阅式 store（不引入状态库）。 */

export interface Store<T> {
  getState(): T;
  setState(updater: Partial<T> | ((state: T) => Partial<T>)): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<() => void>();

  return {
    getState(): T {
      return state;
    },
    setState(updater): void {
      const patch = typeof updater === "function" ? updater(state) : updater;
      if (patch === null || patch === undefined) {
        return;
      }
      let changed = false;
      for (const key of Object.keys(patch) as Array<keyof T>) {
        if (!Object.is(state[key], patch[key])) {
          changed = true;
          break;
        }
      }
      if (!changed) {
        return;
      }
      state = { ...state, ...patch };
      for (const listener of [...listeners]) {
        listener();
      }
    },
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
