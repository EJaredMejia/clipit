// The Review tab: unreviewed clips as a deck of cards, thrown right to keep and left to skip.
import { Check, Play, Undo2, Volume2, VolumeX, X } from "lucide-react";
import { type PointerEvent, type Ref, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { type Clip, clipOf, decide, openClip, set, showTab, startFling, undo, useClipit, type Verdict, VIEWS } from "./store";
import { ClipFacts, FileTools, media } from "./ui";

type CardHandle = { flyOut: (verdict: Verdict, velocity?: number) => void; toggle: () => void };

function toggle(video: HTMLVideoElement): void {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const sign = (v: Verdict) => (v === "keep" ? 1 : -1);
const big =
  "h-14 min-w-0 flex-1 rounded-2xl px-4 sm:min-w-36 sm:flex-none sm:px-6 font-heading text-lg font-bold hover:brightness-110 disabled:opacity-40 [&_svg:not([class*='size-'])]:size-5";

export function Review() {
  const clips = useClipit((s) => s.data.clips);
  const queue = useClipit((s) => s.queue);
  const last = useClipit((s) => s.history.at(-1));
  const loaded = useClipit((s) => s.loaded);
  const top = clipOf(clips, queue[0]);
  const next = clipOf(clips, queue[1]);
  const done = loaded && !top;
  const card = useRef<CardHandle>(null);
  // Keyboard decisions are made over and over: those cards change instantly, with no motion to wait for.
  const [instant, setInstant] = useState(false);
  // a card brought back by Undo comes in from the side it was thrown to
  const returning = useRef<{ id: number; from: number } | null>(null);

  const fling = (verdict: Verdict, how: "key" | "button" | "throw", velocity = 0) => {
    if (!startFling()) return;
    setInstant(how === "key");
    if (how === "key" || reduced()) return void decide(verdict);
    card.current?.flyOut(verdict, velocity);
    setTimeout(() => decide(verdict), 170);
  };
  const back = (how: "key" | "button") => {
    if (!last || useClipit.getState().flinging) return;
    setInstant(how === "key");
    returning.current = how === "key" ? null : { id: last.id, from: sign(last.verdict) };
    void undo();
  };

  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      const target = e.target as Element;
      const s = useClipit.getState();
      // arrows also move through lists, menus and dialogs: only rate when the page itself has the keys
      const busy = target.closest("input, select, textarea, [role=listbox], [role=menu], [role=dialog], [role=alertdialog]");
      if (s.tab !== "review" || s.sheet !== null || e.ctrlKey || e.metaKey || e.altKey || busy) return;
      if (e.key === "ArrowRight") fling("keep", "key");
      else if (e.key === "ArrowLeft") fling("skip", "key");
      else if (e.key === "z" || e.key === "Z" || e.key === "Backspace") back("key");
      else if (e.key === " " && card.current && !target.closest("button")) card.current.toggle();
      else return;
      e.preventDefault();
    };
    document.addEventListener("keydown", keys);
    return () => document.removeEventListener("keydown", keys);
  });
  useEffect(() => {
    returning.current = null; // used by the card it was for (its effects run first), never by a later one
  });

  if (done) return <Done canUndo={Boolean(last)} onUndo={() => back("button")} />;
  return (
    <div id="review" className="grid items-center justify-center gap-x-16 gap-y-8 lg:min-h-[calc(100vh-9rem)] lg:grid-cols-[auto_minmax(0,30rem)]">
      <div className="flex flex-col items-center">
        <div id="deck" aria-live="polite" className="grid place-items-center *:[grid-area:1/1]">
          {next && <div className="deck-card back" aria-hidden="true" />}
          {top && (
            <Card
              key={top.id}
              ref={card}
              clip={top}
              instant={instant}
              from={returning.current?.id === top.id ? returning.current.from : 0}
              onThrow={(v, velocity) => fling(v, "throw", velocity)}
            />
          )}
        </div>
        <div className="mt-6 flex w-full max-w-sm items-center gap-3 sm:w-auto sm:max-w-none">
          <Button type="button" id="skip" className={`${big} bg-skip-bg text-skip`} disabled={!top} onClick={() => fling("skip", "button")}>
            <X strokeWidth={2.75} />
            Skip
          </Button>
          <Button
            type="button"
            id="undo"
            variant="ghost"
            aria-label="Undo"
            className="size-14 shrink-0 rounded-2xl text-muted-foreground"
            disabled={!last}
            onClick={() => back("button")}
          >
            <Undo2 className="size-5" />
          </Button>
          <Button type="button" id="keep" className={`${big} bg-keep-bg text-keep`} disabled={!top} onClick={() => fling("keep", "button")}>
            <Check strokeWidth={2.75} />
            Keep
          </Button>
        </div>
      </div>
      <div id="side" className="w-full max-w-xl justify-self-center">
        <p id="left" className="font-heading text-sm text-muted-foreground tabular-nums">
          {loaded ? `${queue.length} left to review` : "Loading…"}
        </p>
        {top && (
          <>
            <h2 className="mt-3 font-heading text-[clamp(2.1rem,3.6vw,3.25rem)] leading-[1.02] font-bold text-balance">{top.title || "(untitled)"}</h2>
            <ClipFacts clip={top} className="mt-5 text-[0.95rem]" />
            {top.why && <p className="mt-6 max-w-prose text-[1.05rem] leading-relaxed text-muted-foreground">{top.why}</p>}
            <div className="mt-7 flex flex-wrap gap-2">
              <FileTools key={top.id} clip={top} />
              <Button type="button" variant="outline" size="sm" onClick={() => openClip(top.id)}>
                Open details
              </Button>
            </div>
          </>
        )}
        <dl className="mt-12 hidden grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2.5 text-sm text-muted-foreground lg:grid">
          <dt className="flex gap-1">
            <Kbd>←</Kbd>
            <Kbd>→</Kbd>
          </dt>
          <dd>Skip or keep. Or drag the card, a flick is enough.</dd>
          <dt>
            <Kbd>Z</Kbd>
          </dt>
          <dd>Undo the last one</dd>
          <dt>
            <Kbd>Space</Kbd>
          </dt>
          <dd>Pause or play</dd>
        </dl>
      </div>
    </div>
  );
}

// Nothing left to review: the next step takes the stage.
function Done({ canUndo, onUndo }: { canUndo: boolean; onUndo: () => void }) {
  const clips = useClipit((s) => s.data.clips);
  const kept = clips.filter(VIEWS.ready[1]).length;
  const any = clips.length > 0;
  return (
    <div className="mx-auto flex max-w-xl flex-col items-start py-[12vh]">
      <h2 className="font-heading text-[clamp(2.5rem,5vw,4rem)] leading-none font-bold">{any ? "All caught up" : "No clips yet"}</h2>
      <p className="mt-4 text-lg text-muted-foreground">
        {any
          ? kept
            ? `${kept} kept clip${kept === 1 ? " is" : "s are"} waiting to be posted. New clips show up here as clipit makes them.`
            : "New clips show up here as clipit makes them."
          : "Run clipit on a VOD, or clipit --poll, and its clips show up here to review."}
      </p>
      <div className="mt-8 flex gap-3">
        {kept > 0 && (
          <Button type="button" className="h-12 rounded-xl px-6 font-heading text-base font-bold" onClick={() => showTab("post")}>
            {`Post ${kept === 1 ? "it" : `the ${kept} clips`}`}
          </Button>
        )}
        {/* the last card's decision stays reversible */}
        {canUndo && (
          <Button type="button" variant="ghost" className="h-12 rounded-xl px-4 font-heading text-base text-muted-foreground" onClick={onUndo}>
            <Undo2 />
            Undo the last one
          </Button>
        )}
      </div>
    </div>
  );
}

function Card(props: {
  clip: Clip;
  instant: boolean;
  from: number; // -1 / 1: it was thrown that way and comes back from there (Undo); 0: promoted from behind
  onThrow: (verdict: Verdict, velocity: number) => void;
  ref: Ref<CardHandle>;
}) {
  const { clip: c, instant, from, onThrow, ref } = props;
  const el = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const keep = useRef<HTMLDivElement>(null);
  const skip = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLElement>(null);
  const drag = useRef({ x0: null as number | null, t0: 0, dx: 0, moves: [] as { x: number; t: number }[] });
  const muted = useClipit((s) => s.muted); // shared by every card this visit
  const sheetOpen = useClipit((s) => s.sheet !== null);
  const released = useClipit((s) => s.released.includes(c.id));
  const [paused, setPaused] = useState(false);
  const [dragging, setDragging] = useState(false);

  // the card follows the pointer 1:1 from where it was grabbed, so its transform is set directly, frame by frame
  const move = (x: number) => {
    drag.current.dx = x;
    el.current!.style.transform = x ? `translateX(${x}px) rotate(${x / 18}deg)` : "";
    keep.current!.style.opacity = String(Math.min(1, Math.max(0, x / 100)));
    skip.current!.style.opacity = String(Math.min(1, Math.max(0, -x / 100)));
  };

  useImperativeHandle(ref, () => ({
    // Thrown off the side, carrying the release speed: a hard flick leaves faster than a slow drag past the line.
    flyOut(verdict, velocity = 0) {
      const card = el.current!;
      const s = sign(verdict);
      const target = s * (innerWidth / 2 + card.offsetWidth + 40);
      const ms = Math.round(Math.min(300, Math.max(160, (3 * Math.abs(target - drag.current.dx)) / Math.max(Math.abs(velocity), 0.8))));
      keep.current!.style.opacity = s > 0 ? "1" : "0"; // a button press shows the stamp on the way out too
      skip.current!.style.opacity = s < 0 ? "1" : "0";
      card.style.transition = `transform ${ms}ms cubic-bezier(0.2, 0.7, 0.4, 1), opacity ${ms}ms ease-out`;
      card.style.transform = `translateX(${target}px) rotate(${s * 22}deg)`;
      card.style.opacity = "0";
    },
    toggle: () => toggle(video.current!),
  }));

  // Undo: back in from the side it left by, settling like the throw in reverse
  useEffect(() => {
    if (!from || reduced()) return;
    const w = el.current!.offsetWidth;
    el.current!.animate(
      [{ transform: `translateX(${from * (innerWidth / 2 + w)}px) rotate(${from * 22}deg)`, opacity: 0 }, { transform: "none", opacity: 1 }],
      { duration: 280, easing: "cubic-bezier(0.32, 0.72, 0, 1)" },
    );
  }, []);
  useEffect(() => {
    if (video.current) video.current.muted = muted;
  }, [muted]);
  useEffect(() => {
    if (sheetOpen) video.current?.pause(); // the details sheet plays this clip itself
  }, [sheetOpen]);
  useEffect(() => {
    const v = video.current;
    if (!v) return;
    const s = useClipit.getState();
    if (s.tab !== "review" || s.sheet !== null) return setPaused(true); // another tab, or the sheet is playing
    v.play().catch(() => {
      // the browser blocks autoplay with sound until the page has been clicked: start muted instead
      v.muted = true;
      set({ muted: true });
      v.play().catch(() => setPaused(true));
    });
  }, [released]);

  const down = (e: PointerEvent<HTMLDivElement>) => {
    if (!e.isPrimary || e.button !== 0 || (e.target as Element).closest("button") || useClipit.getState().flinging) return;
    drag.current = { x0: e.clientX, t0: e.timeStamp, dx: 0, moves: [{ x: e.clientX, t: e.timeStamp }] };
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
    e.currentTarget.style.transition = keep.current!.style.transition = skip.current!.style.transition = "none"; // grabbed: follow at once
  };
  const track = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (d.x0 === null || !e.isPrimary) return;
    d.moves = [...d.moves.filter((m) => e.timeStamp - m.t < 80), { x: e.clientX, t: e.timeStamp }]; // the last 80 ms: the release speed
    move(e.clientX - d.x0);
  };
  const end = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current.x0 === null || !e.isPrimary) return false;
    drag.current.x0 = null;
    setDragging(false);
    return true;
  };
  // let go short of a decision: spring back home, a little past center, the way it was thrown
  const home = (card: HTMLDivElement) => {
    card.style.transition = reduced() ? "none" : "transform 280ms cubic-bezier(0.34, 1.45, 0.64, 1)";
    keep.current!.style.transition = skip.current!.style.transition = "opacity 200ms ease-out";
    move(0);
  };
  const up = (e: PointerEvent<HTMLDivElement>) => {
    if (!end(e)) return;
    const { dx, t0, moves } = drag.current;
    const first = moves[0];
    const velocity = moves.length > 1 ? (e.clientX - first.x) / Math.max(1, e.timeStamp - first.t) : 0; // px per ms
    // a flick decides by its direction, however short; a slow drag by how far it went
    const flick = Math.abs(velocity) > 0.45 && Math.abs(dx) > 24 && Math.sign(velocity) === Math.sign(dx);
    if (flick || Math.abs(dx) > Math.min(120, e.currentTarget.offsetWidth / 3)) return onThrow(dx > 0 ? "keep" : "skip", velocity);
    if (Math.abs(dx) < 6 && e.timeStamp - t0 < 400) toggle(video.current!); // a tap, not a drag
    home(e.currentTarget);
  };
  // the browser took the gesture over (a scroll): put the card back, never rate it
  const cancel = (e: PointerEvent<HTMLDivElement>) => end(e) && home(e.currentTarget);

  return (
    <div
      ref={el}
      className={`deck-card outline-offset-4${instant ? " instant" : ""}${dragging ? " dragging" : ""}`}
      tabIndex={0}
      aria-label={`${c.title || "Untitled clip"}. Right arrow keeps it, left arrow skips it.`}
      onPointerDown={down}
      onPointerMove={track}
      onPointerUp={up}
      onPointerCancel={cancel}
    >
      {!released && (
        <video
          ref={video}
          src={media(c)}
          loop
          playsInline
          preload="auto"
          onTimeUpdate={(e) => (bar.current!.style.transform = `scaleX(${e.currentTarget.currentTime / e.currentTarget.duration || 0})`)}
          onPlay={() => setPaused(false)}
          onPause={() => setPaused(true)}
        />
      )}
      {paused && (
        <span className="pointer-events-none absolute top-1/2 left-1/2 grid size-18 -translate-1/2 place-items-center rounded-full bg-black/45 pl-1 text-white backdrop-blur-md">
          <Play className="size-7 fill-current" />
        </span>
      )}
      <div ref={keep} className="deck-stamp keep">
        KEEP
      </div>
      <div ref={skip} className="deck-stamp skip">
        SKIP
      </div>
      <button
        type="button"
        className="absolute right-3 bottom-4 z-10 flex items-center gap-1.5 rounded-full bg-black/55 px-3 py-1.5 font-heading text-sm font-semibold text-white backdrop-blur-md transition-transform duration-150 ease-out active:scale-[0.95]"
        aria-pressed={!muted}
        onClick={() => set({ muted: !muted })}
      >
        {muted ? <VolumeX className="size-4" /> : <Volume2 className="size-4" />}
        {muted ? "Sound off" : "Sound on"}
      </button>
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-1 bg-white/20">
        <i ref={bar} className="block h-full origin-left scale-x-0 bg-primary" />
      </div>
    </div>
  );
}
