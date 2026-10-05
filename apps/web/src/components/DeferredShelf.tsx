import { useEffect, useRef, useState, type ReactNode } from "react";

/** Reserve shelf space and start its requests only as it approaches the viewport. */
export default function DeferredShelf({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "400px" },
    );
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  return (
    <div ref={host} className={visible ? undefined : "deferred-shelf"}>
      {visible ? children : <h2>{title}</h2>}
    </div>
  );
}
