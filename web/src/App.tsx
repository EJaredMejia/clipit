import { GalleryVerticalEnd, LayoutGrid, type LucideIcon, MonitorPlay, Play, Send } from "lucide-react";
import { useEffect, useRef } from "react";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Toaster } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ClipEditor } from "./Editor";
import { Library } from "./Library";
import { Post } from "./Post";
import { Review } from "./Review";
import { Run } from "./Run";
import { clipOf, load, loadRun, openClip, POLL_MS, postQueue, RUN_MS, showTab, type Tab, TABS, useClipit } from "./store";
import { Vods } from "./Vods";

const ICONS: Record<Tab, LucideIcon> = { run: Play, review: GalleryVerticalEnd, post: Send, library: LayoutGrid, vods: MonitorPlay };

export function App() {
  const tab = useClipit((s) => s.tab);

  useEffect(() => {
    document.addEventListener("visibilitychange", load);
    const poll = setInterval(load, POLL_MS);
    const runs = setInterval(loadRun, RUN_MS);
    void load();
    void loadRun();
    return () => {
      document.removeEventListener("visibilitychange", load);
      clearInterval(poll);
      clearInterval(runs);
    };
  }, []);
  // the browser tab says when clips are waiting, so a background tab still tells you
  const waiting = useClipit((s) => s.queue.length + postQueue(s).length);
  const running = useClipit((s) => !!s.run?.running);
  useEffect(() => {
    document.title = `${waiting ? `(${waiting}) ` : ""}clip-it${running ? " · running" : ""}`;
  }, [waiting, running]);
  useEffect(() => {
    for (const v of document.querySelectorAll<HTMLVideoElement>("[data-panel][hidden] video")) v.pause();
    scrollTo({ top: 0 });
  }, [tab]);

  return (
    <TooltipProvider delay={500}>
      <header className="chrome sticky top-0 z-30 border-b border-border/60">
        <div className="mx-auto flex h-16 max-w-[1440px] items-center gap-6 px-4 sm:px-6">
          <h1 className="font-heading text-[1.4rem] font-bold">clip-it</h1>
          <Nav place="top" />
          <Tally />
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1440px] px-4 pt-6 pb-32 sm:px-6 md:pt-8 md:pb-16">
        {(Object.keys(TABS) as Tab[]).map((t) => (
          // every tab stays mounted, so the deck's video and the Library's filters survive switching
          <div key={t} data-panel={t} hidden={tab !== t} aria-label={TABS[t]}>
            {t === "run" ? <Run /> : t === "review" ? <Review /> : t === "post" ? <Post /> : t === "library" ? <Library /> : <Vods />}
          </div>
        ))}
      </main>
      <Nav place="bottom" />
      <ClipSheet />
      <Toaster position="bottom-right" mobileOffset={{ bottom: 88 }} />
    </TooltipProvider>
  );
}

// What waits on you in each section, next to its name: clips to review, clips to post, VODs that failed.
function useCounts(): Record<Tab, number> {
  return {
    run: 0, // a dot while running, below
    review: useClipit((s) => s.queue.length),
    post: useClipit((s) => postQueue(s).length),
    library: 0,
    vods: useClipit((s) => s.data.vods.filter((v) => v.status === "failed").length),
  };
}

// Page navigation, not tabs: each section is a place. A pill bar up top on wide screens, a tab bar at the thumb on phones.
function Nav({ place }: { place: "top" | "bottom" }) {
  const tab = useClipit((s) => s.tab);
  const loaded = useClipit((s) => s.loaded);
  const counts = useCounts();
  const running = useClipit((s) => !!s.run?.running);
  const badge = (t: Tab) =>
    t === "run" && running ? (
      <span className="size-2 rounded-full bg-warn" role="img" aria-label="running" />
    ) : loaded && counts[t] > 0 ? (
      <span
        className={`min-w-5 rounded-full px-1.5 text-center font-heading text-xs leading-5 font-bold tabular-nums ${t === "vods" ? "bg-skip-bg text-skip" : "bg-primary/15 text-primary"}`}
        aria-label={t === "vods" ? `${counts[t]} failed` : `${counts[t]} waiting`}
      >
        {counts[t]}
      </span>
    ) : null;

  if (place === "top") {
    return (
      <nav aria-label="Sections" className="hidden items-center gap-1 rounded-full bg-muted/70 p-1 md:flex">
        {(Object.keys(TABS) as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            aria-current={tab === t ? "page" : undefined}
            onClick={() => showTab(t)}
            className="flex h-9 items-center gap-2 rounded-full px-4 font-heading text-[0.95rem] font-semibold text-muted-foreground transition-[color,background-color,transform] duration-150 ease-out hover:text-foreground active:scale-[0.97] aria-[current=page]:bg-card aria-[current=page]:text-foreground aria-[current=page]:shadow-sm"
          >
            {TABS[t]}
            {badge(t)}
          </button>
        ))}
      </nav>
    );
  }
  return (
    <nav
      aria-label="Sections"
      className="chrome fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-border/60 pb-[env(safe-area-inset-bottom)] md:hidden"
    >
      {(Object.keys(TABS) as Tab[]).map((t) => {
        const Icon = ICONS[t];
        return (
          <button
            key={t}
            type="button"
            aria-current={tab === t ? "page" : undefined}
            onClick={() => showTab(t)}
            className="group relative flex h-16 flex-col items-center justify-center gap-1 font-heading text-[0.7rem] font-semibold text-muted-foreground active:scale-[0.95] aria-[current=page]:text-foreground"
          >
            <Icon className="size-[1.4rem] group-aria-[current=page]:text-primary" strokeWidth={2} />
            {TABS[t]}
            <span className="absolute top-1.5 left-1/2 ml-2">{badge(t)}</span>
          </button>
        );
      })}
    </nav>
  );
}

// The connection light: quiet while the page keeps up with the server, says what's wrong when it can't.
function Tally() {
  const { text, bad } = useClipit((s) => s.status);
  const loaded = useClipit((s) => s.loaded);
  if (bad) {
    return (
      <p id="status" role="status" className="ml-auto flex items-center gap-2 rounded-full bg-skip-bg px-3 py-1.5 text-sm font-medium text-skip">
        <span className="size-2 shrink-0 rounded-full bg-skip" aria-hidden="true" />
        <span className="line-clamp-1">{text}</span>
      </p>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger
        render={<p id="status" role="status" tabIndex={0} className="ml-auto flex items-center gap-2 rounded-full px-2 py-1 text-sm text-muted-foreground" />}
      >
        <span className={`size-2 rounded-full ${loaded ? "bg-keep shadow-[0_0_0_3px_color-mix(in_srgb,var(--keep)_20%,transparent)]" : "bg-muted-foreground/40"}`} aria-hidden="true" />
        <span className={loaded ? "sr-only" : undefined}>{loaded ? "Connected" : text}</span>
      </TooltipTrigger>
      <TooltipContent side="bottom">{loaded ? "Connected. New clips show up on their own." : "Loading"}</TooltipContent>
    </Tooltip>
  );
}

// Any clip, opened over whatever tab you're on (from the Library or the deck's Open details). Slides in from the
// right and back out the same way; closing is quicker than opening.
function ClipSheet() {
  const open = useClipit((s) => clipOf(s.data.clips, s.sheet));
  const last = useRef(open);
  if (open) last.current = open; // keep showing it while the sheet slides out
  const shown = open ?? last.current;
  return (
    <Sheet open={Boolean(open)} onOpenChange={(o) => !o && openClip(null)}>
      <SheetContent
        side="right"
        className="gap-0 overflow-hidden bg-background p-0 duration-280 ease-(--ease-drawer) data-ending-style:duration-200 data-ending-style:opacity-100 data-starting-style:opacity-100 data-[side=right]:w-full data-[side=right]:data-ending-style:translate-x-full data-[side=right]:data-starting-style:translate-x-full data-[side=right]:sm:max-w-[min(72rem,94vw)] motion-reduce:duration-0"
      >
        <SheetTitle className="sr-only">{shown?.title || "Clip"}</SheetTitle>
        {shown && <ClipEditor key={shown.id} clip={shown} layout="sheet" />}
      </SheetContent>
    </Sheet>
  );
}
