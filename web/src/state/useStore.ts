import { useEffect, useRef, useState } from "preact/hooks";
import type { Store } from "./store";

/**
 * 订阅 store 并只取需要的切片。
 *
 * `selector` 必须是稳定引用（模块级函数或 `useCallback`）—— 否则每次渲染都会
 * 重新订阅，白白抖动。
 */
export function useStore<T extends object, S>(store: Store<T>, selector: (state: T) => S): S {
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  const [selected, setSelected] = useState<S>(() => selector(store.getState()));

  useEffect(() => {
    const update = () => {
      const next = selectorRef.current(store.getState());
      setSelected((previous) => (Object.is(previous, next) ? previous : next));
    };
    update();
    return store.subscribe(update);
  }, [store]);

  return selected;
}
