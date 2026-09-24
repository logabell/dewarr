import { Fragment, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check } from "lucide-react";

type MenuOption = {
  value: string;
  label: string;
  disabled: boolean;
  group?: string;
};

type MenuState = {
  select: HTMLSelectElement;
  options: MenuOption[];
  active: number;
  label: string;
  moveFocus: boolean;
};

const MENU_ID = "app-select-menu";

function customMenusSupported() {
  return "showPopover" in HTMLElement.prototype;
}

function eligible(select: HTMLSelectElement | null) {
  if (!select || select.multiple || select.size > 1 || select.disabled)
    return null;
  if (window.matchMedia("(forced-colors: active)").matches) return null;
  return select;
}

function readOptions(select: HTMLSelectElement): MenuOption[] {
  return [...select.options].map((option) => {
    const parent = option.parentElement;
    const group =
      parent instanceof HTMLOptGroupElement && parent.label
        ? parent.label
        : undefined;
    return {
      value: option.value,
      label: option.label || option.value,
      disabled: option.disabled,
      group,
    };
  });
}

function selectName(select: HTMLSelectElement) {
  const named = select.getAttribute("aria-label")?.trim();
  if (named) return named;
  const label = select.labels?.[0];
  if (!label) return "Options";
  const copy = label.cloneNode(true) as HTMLElement;
  copy
    .querySelectorAll("select, input, textarea, button")
    .forEach((node) => node.remove());
  return copy.textContent?.replace(/\s+/g, " ").trim() || "Options";
}

function place(select: HTMLSelectElement, menu: HTMLElement) {
  const rect = select.getBoundingClientRect();
  const gap = 4;
  const margin = 8;
  const available = Math.max(0, window.innerWidth - margin * 2);
  menu.style.position = "fixed";
  menu.style.margin = "0";
  menu.style.inset = "auto";
  menu.style.right = "auto";
  menu.style.bottom = "auto";
  menu.style.minWidth = `${Math.min(rect.width, available)}px`;
  menu.style.maxWidth = `${available}px`;
  menu.style.width = "max-content";
  const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
  const spaceAbove = rect.top - gap - margin;
  const openUp = spaceBelow < 160 && spaceAbove > spaceBelow;
  menu.style.maxHeight = `${Math.max(80, Math.min(320, openUp ? spaceAbove : spaceBelow))}px`;
  const width = menu.getBoundingClientRect().width;
  let left = rect.left;
  if (left + width > window.innerWidth - margin) left = rect.right - width;
  left = Math.max(margin, Math.min(left, window.innerWidth - margin - width));
  menu.style.left = `${left}px`;
  if (openUp) {
    const height = menu.getBoundingClientRect().height;
    menu.style.top = `${Math.max(margin, rect.top - gap - height)}px`;
  } else {
    menu.style.top = `${rect.bottom + gap}px`;
  }
}

function reveal(option: HTMLElement | null, menu: HTMLElement) {
  if (!option) return;
  const top = option.offsetTop;
  const bottom = top + option.offsetHeight;
  if (top < menu.scrollTop) menu.scrollTop = top;
  else if (bottom > menu.scrollTop + menu.clientHeight)
    menu.scrollTop = bottom - menu.clientHeight;
}

function optionId(index: number) {
  return `app-select-option-${index}`;
}

export default function SelectMenu() {
  const popoverRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<MenuState | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);

  const sync = (next: MenuState | null) => {
    menuRef.current = next;
    setMenu(next);
  };

  const close = (restore: boolean) => {
    const current = menuRef.current;
    if (!current) return;
    current.select.removeAttribute("data-open");
    current.select.removeAttribute("aria-expanded");
    current.select.removeAttribute("aria-controls");
    current.select.removeAttribute("aria-activedescendant");
    sync(null);
    const pop = popoverRef.current;
    if (pop?.matches(":popover-open")) pop.hidePopover();
    if (restore) current.select.focus({ preventScroll: true });
  };

  const open = (select: HTMLSelectElement, moveFocus: boolean) => {
    const options = readOptions(select);
    const active = options.findIndex(
      (option) => option.value === select.value && !option.disabled,
    );
    const fallback = options.findIndex((option) => !option.disabled);
    const index = active >= 0 ? active : fallback;
    if (index < 0) return;
    const previous = menuRef.current;
    if (previous && previous.select !== select) {
      previous.select.removeAttribute("data-open");
      previous.select.removeAttribute("aria-expanded");
      previous.select.removeAttribute("aria-controls");
      previous.select.removeAttribute("aria-activedescendant");
    }
    select.dataset.open = "true";
    select.setAttribute("aria-expanded", "true");
    select.setAttribute("aria-controls", MENU_ID);
    select.setAttribute("aria-activedescendant", optionId(index));
    sync({
      select,
      options,
      active: index,
      label: selectName(select),
      moveFocus,
    });
  };

  const updateActive = (active: number, moveFocus = true) => {
    const current = menuRef.current;
    if (!current || current.active === active) return;
    current.select.setAttribute("aria-activedescendant", optionId(active));
    sync({ ...current, active, moveFocus });
  };

  const commit = (value: string) => {
    const current = menuRef.current;
    if (!current) return;
    const option = current.options.find((item) => item.value === value);
    if (!option || option.disabled) return;
    const select = current.select;
    if (select.value !== value) {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    close(true);
  };

  useLayoutEffect(() => {
    const pop = popoverRef.current;
    if (!pop) return;
    if (!menu) {
      if (pop.matches(":popover-open")) pop.hidePopover();
      return;
    }
    if (!menu.select.isConnected) {
      close(false);
      return;
    }
    if (!pop.matches(":popover-open")) {
      try {
        pop.showPopover();
      } catch {
        return;
      }
    }
    place(menu.select, pop);
    const option = pop.querySelector<HTMLElement>(
      `[data-index="${menu.active}"]`,
    );
    reveal(option, pop);
    if (menu.moveFocus || pop.contains(document.activeElement))
      option?.focus({ preventScroll: true });
    const follow = (event: Event) => {
      if (event.target instanceof Node && pop.contains(event.target)) return;
      if (!menu.select.isConnected) {
        close(false);
        return;
      }
      place(menu.select, pop);
    };
    document.addEventListener("scroll", follow, true);
    window.addEventListener("resize", follow);
    return () => {
      document.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
    };
  }, [menu]);

  useEffect(() => {
    if (!customMenusSupported()) return;
    let suppressUntil = 0;
    let buffer = "";
    let bufferTimer = 0;

    const targetSelect = (event: Event) => {
      if (!(event.target instanceof HTMLSelectElement)) return null;
      if (popoverRef.current?.contains(event.target)) return null;
      return eligible(event.target);
    };

    const step = (direction: 1 | -1) => {
      const current = menuRef.current;
      if (!current) return;
      for (
        let index = current.active + direction;
        index >= 0 && index < current.options.length;
        index += direction
      ) {
        if (!current.options[index].disabled) {
          updateActive(index);
          return;
        }
      }
    };

    const jump = (end: boolean) => {
      const current = menuRef.current;
      if (!current) return;
      const indexes = current.options.flatMap((option, index) =>
        option.disabled ? [] : [index],
      );
      if (!indexes.length) return;
      updateActive(end ? indexes[indexes.length - 1] : indexes[0]);
    };

    const page = (direction: 1 | -1) => {
      const current = menuRef.current;
      if (!current) return;
      let index = current.active;
      let remaining = 8;
      while (remaining > 0) {
        const next = index + direction;
        if (next < 0 || next >= current.options.length) break;
        index = next;
        if (!current.options[index].disabled) remaining -= 1;
      }
      if (!current.options[index]?.disabled) updateActive(index);
    };

    const typeahead = (text: string) => {
      const current = menuRef.current;
      if (!current) return;
      buffer += text.toLowerCase();
      window.clearTimeout(bufferTimer);
      bufferTimer = window.setTimeout(() => {
        buffer = "";
      }, 500);
      const start = buffer.length > 1 ? current.active : current.active + 1;
      for (let offset = 0; offset < current.options.length; offset += 1) {
        const index = (start + offset) % current.options.length;
        const option = current.options[index];
        if (option.disabled) continue;
        if (option.label.toLowerCase().startsWith(buffer)) {
          updateActive(index);
          return;
        }
      }
    };

    const onMouseDown = (event: MouseEvent) => {
      const select = targetSelect(event);
      if (!select || event.button !== 0) return;
      event.preventDefault();
    };

    const labeledSelect = (event: Event) => {
      if (!(event.target instanceof Element)) return null;
      if (event.target instanceof HTMLSelectElement) return null;
      if (popoverRef.current?.contains(event.target)) return null;
      const label = event.target.closest("label");
      if (!(label instanceof HTMLLabelElement)) return null;
      return eligible(
        label.control instanceof HTMLSelectElement ? label.control : null,
      );
    };

    const onPointerDown = (event: PointerEvent) => {
      const select = targetSelect(event) || labeledSelect(event);
      if (!select || event.button !== 0) return;
      event.preventDefault();
      if (menuRef.current?.select === select) {
        suppressUntil = performance.now() + 400;
        close(false);
        return;
      }
      if (menuRef.current) close(false);
    };

    const onPointerUp = (event: PointerEvent) => {
      const select = targetSelect(event) || labeledSelect(event);
      if (!select || event.button !== 0) return;
      if (performance.now() < suppressUntil) return;
      if (menuRef.current?.select === select) return;
      select.focus({ preventScroll: true });
      open(select, false);
    };

    const onClick = (event: MouseEvent) => {
      if (performance.now() < suppressUntil) return;
      if (!(event.target instanceof Element)) return;
      if (event.target instanceof HTMLSelectElement) return;
      if (popoverRef.current?.contains(event.target)) return;
      const label = event.target.closest("label");
      if (!(label instanceof HTMLLabelElement)) return;
      const select = eligible(
        label.control instanceof HTMLSelectElement ? label.control : null,
      );
      if (!select || menuRef.current?.select === select) return;
      event.preventDefault();
      select.focus({ preventScroll: true });
      open(select, false);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const current = menuRef.current;
      const onSelect =
        event.target instanceof HTMLSelectElement
          ? eligible(event.target)
          : null;
      const inMenu =
        event.target instanceof Node &&
        !!popoverRef.current?.contains(event.target);
      if (!current) {
        if (!onSelect) return;
        const openKey =
          (event.altKey && event.key === "ArrowDown") ||
          (!event.metaKey &&
            !event.ctrlKey &&
            !event.altKey &&
            (event.key === "ArrowDown" ||
              event.key === "ArrowUp" ||
              event.key === " "));
        if (!openKey) return;
        event.preventDefault();
        event.stopPropagation();
        open(onSelect, true);
        if (event.key === "ArrowDown") step(1);
        else if (event.key === "ArrowUp") step(-1);
        return;
      }
      if (!inMenu && onSelect !== current.select) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close(true);
        return;
      }
      if (event.key === "Tab") {
        close(true);
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        step(1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        step(-1);
        return;
      }
      if (event.key === "Home") {
        event.preventDefault();
        jump(false);
        return;
      }
      if (event.key === "End") {
        event.preventDefault();
        jump(true);
        return;
      }
      if (event.key === "PageDown") {
        event.preventDefault();
        page(1);
        return;
      }
      if (event.key === "PageUp") {
        event.preventDefault();
        page(-1);
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        commit(current.options[current.active]?.value ?? "");
        return;
      }
      if (
        event.key.length === 1 &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey
      ) {
        event.preventDefault();
        typeahead(event.key);
      }
    };

    document.addEventListener("mousedown", onMouseDown, {
      capture: true,
      passive: false,
    });
    document.addEventListener("pointerdown", onPointerDown, {
      capture: true,
      passive: false,
    });
    document.addEventListener("pointerup", onPointerUp, true);
    document.addEventListener("click", onClick, true);
    const onKeyUp = (event: KeyboardEvent) => {
      if (!(event.target instanceof HTMLSelectElement)) return;
      if (!eligible(event.target)) return;
      if (
        event.key === " " ||
        event.key === "ArrowDown" ||
        event.key === "ArrowUp"
      )
        event.preventDefault();
    };

    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("keyup", onKeyUp, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown, true);
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointerup", onPointerUp, true);
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("keyup", onKeyUp, true);
      window.clearTimeout(bufferTimer);
    };
  }, []);

  let previousGroup: string | undefined;
  // A modal makes DOM outside it inert, even when a popover is visually above it.
  // Keep options inside the owning dialog so pointer and focus events reach them.
  return createPortal(
    <div
      ref={popoverRef}
      id={MENU_ID}
      popover="auto"
      role="listbox"
      aria-label={menu?.label || "Options"}
      className="app-select-menu"
      onToggle={(event) => {
        if (event.newState !== "closed" || !menuRef.current) return;
        menuRef.current.select.removeAttribute("data-open");
        menuRef.current.select.removeAttribute("aria-expanded");
        menuRef.current.select.removeAttribute("aria-controls");
        menuRef.current.select.removeAttribute("aria-activedescendant");
        sync(null);
      }}
      onMouseDown={(event) => event.preventDefault()}
    >
      {menu?.options.map((option, index) => {
        const showGroup = option.group && option.group !== previousGroup;
        previousGroup = option.group;
        const selected = option.value === menu.select.value;
        return (
          <Fragment key={index}>
            {showGroup && (
              <div className="app-select-group">{option.group}</div>
            )}
            <button
              type="button"
              id={optionId(index)}
              role="option"
              data-index={index}
              data-active={index === menu.active ? "true" : undefined}
              aria-selected={selected}
              disabled={option.disabled}
              className="app-select-option"
              title={option.label}
              onMouseEnter={() => {
                if (!option.disabled) updateActive(index, false);
              }}
              onClick={() => commit(option.value)}
            >
              <span className="app-select-check" aria-hidden="true">
                {selected && <Check size={14} />}
              </span>
              <span className="app-select-label">{option.label}</span>
            </button>
          </Fragment>
        );
      })}
    </div>,
    menu?.select.closest("dialog") || document.body,
  );
}
