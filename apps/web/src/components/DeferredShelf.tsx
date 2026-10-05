import { useEffect, useRef, useState, type ReactNode } from "react";

/** Start leading shelves together, and load later shelves ahead of scrolling. */
export default function DeferredShelf({
  children,
  title,
  immediate = false,
}: {
  children: ReactNode;
  title: string;
  immediate?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(immediate);
  useEffect(() => {
    if (immediate) setVisible(true);
    if (immediate || visible) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "1200px 0px" },
    );
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, [immediate, visible]);
  const mounted = immediate || visible;
  return (
    <div ref={host} className={mounted ? undefined : "deferred-shelf"}>
      {mounted ? children : <h2>{title}</h2>}
    </div>
  );
}
