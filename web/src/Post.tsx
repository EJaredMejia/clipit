// The Post tab: kept clips waiting to go up, one at a time. Marking the last platform posted moves on to the next.
import { Check } from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import { Button } from "@/components/ui/button";
import { ClipEditor } from "./Editor";
import { type Clip, currentPost, PLATFORMS, type Platform, postQueue, set, showTab, useClipit } from "./store";
import { Frame, hot, score } from "./ui";

export function Post() {
  const queue = useClipit(useShallow((s) => postQueue(s)));
  const current = useClipit((s) => currentPost(s));
  const loaded = useClipit((s) => s.loaded);
  const toReview = useClipit((s) => s.queue.length);

  if (loaded && !current) {
    return (
      <div className="mx-auto flex max-w-xl flex-col items-start py-[12vh]">
        <h2 className="font-heading text-[clamp(2.5rem,5vw,4rem)] leading-none font-bold">Nothing to post</h2>
        <p className="mt-4 text-lg text-muted-foreground">
          {toReview
            ? `Clips you keep in Review wait here until they're up on TikTok and YouTube. ${toReview} still to review.`
            : "Clips you keep in Review wait here until they're up on TikTok and YouTube."}
        </p>
        {toReview > 0 && (
          <Button type="button" className="mt-8 h-12 rounded-xl px-6 font-heading text-base font-bold" onClick={() => showTab("review")}>
            {`Review ${toReview === 1 ? "it" : `the ${toReview} clips`}`}
          </Button>
        )}
      </div>
    );
  }
  return (
    <div className="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start xl:gap-10">
      <nav aria-label="Clips to post" className="min-w-0 lg:sticky lg:top-24">
        <p className="font-heading text-sm text-muted-foreground tabular-nums">{loaded ? `${queue.length} to post` : "Loading…"}</p>
        <ol className="-mx-4 mt-2.5 flex gap-2 overflow-x-auto px-4 pb-2 max-lg:[scrollbar-width:none] lg:mx-0 lg:max-h-[calc(100vh-10rem)] lg:flex-col lg:gap-1 lg:overflow-x-visible lg:overflow-y-auto lg:px-0 lg:pb-0">
          {queue.map((c) => (
            <li key={c.id} className="shrink-0">
              <QueueItem clip={c} current={c.id === current?.id} />
            </li>
          ))}
        </ol>
      </nav>
      {current && (
        <section className="min-w-0" aria-label="Clip to post">
          <ClipEditor key={current.id} clip={current} layout="page" />
        </section>
      )}
    </div>
  );
}

function QueueItem({ clip: c, current }: { clip: Clip; current: boolean }) {
  return (
    <button
      type="button"
      aria-current={current ? "true" : undefined}
      onClick={() => set({ current: c.id })}
      className="group flex items-center gap-3 rounded-xl p-1.5 text-left transition-colors duration-150 hover:bg-muted aria-[current=true]:bg-card aria-[current=true]:shadow-sm aria-[current=true]:ring-1 aria-[current=true]:ring-border lg:w-full lg:pr-3"
    >
      <span className="relative block w-14 shrink-0 overflow-hidden rounded-lg ring-2 ring-transparent group-aria-[current=true]:ring-primary lg:w-11">
        <Frame clip={c} />
        <span
          className={`absolute top-1 left-1 rounded bg-black/60 px-1 font-heading text-[0.7rem] leading-4 font-bold tabular-nums backdrop-blur-sm ${hot(c) ? "text-primary" : "text-white"}`}
        >
          {score(c.score)}
        </span>
      </span>
      <span className="hidden min-w-0 flex-col gap-1 lg:flex">
        <span className="line-clamp-2 font-heading text-[0.92rem] leading-tight font-semibold">{c.title || "(untitled)"}</span>
        <span className="flex gap-2.5">
          {(Object.keys(PLATFORMS) as Platform[]).map((p) => (
            <Done key={p} clip={c} platform={p} />
          ))}
        </span>
      </span>
    </button>
  );
}

// a platform's step at a glance: checked once it's up there
function Done({ clip: c, platform }: { clip: Clip; platform: Platform }) {
  const up = c.uploads[platform];
  return (
    <span className={`flex items-center gap-1 text-xs ${up ? "text-keep" : "text-muted-foreground"}`}>
      {up ? <Check className="size-3" strokeWidth={3} /> : <span className="size-1.5 rounded-full bg-current opacity-60" aria-hidden="true" />}
      {PLATFORMS[platform]}
    </span>
  );
}
