// The Library: every clip as its own frame. Hover plays it silently; a click opens it in the side sheet.
import { Check, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { flushSync } from "react-dom";
import { toast } from "sonner";
import { useShallow } from "zustand/react/shallow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  type Clip,
  DELETE_NOTE,
  deleteSkipped,
  LIBRARY_VIEWS,
  openClip,
  type Platform,
  PLATFORMS,
  release,
  set,
  type Sort,
  streamers,
  useClipit,
  type View,
  VIEWS,
  visible,
} from "./store";
import { Confirm, day, Frame, hot, score, secs } from "./ui";

const ALL = "__all"; // a Select value can't be empty
const SORT_LABELS: Record<Sort, string> = { new: "Newest first", score: "Best AI score", streamer: "By streamer" };

export function Library() {
  const clips = useClipit((s) => s.data.clips);
  const rows = useClipit(useShallow(visible));
  const { view, q, streamer, sort, loaded } = useClipit(useShallow(({ view, q, streamer, sort, loaded }) => ({ view, q, streamer, sort, loaded })));
  const names = useMemo(() => streamers(clips), [clips]);
  const streamerItems = useMemo(() => [{ value: ALL, label: "All streamers" }, ...names.map((n) => ({ value: n, label: n }))], [names]);

  return (
    <>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <ToggleGroup value={[view]} onValueChange={(v) => v.length && set({ view: v[0] as View })} spacing={1.5} aria-label="Show clips" className="flex-wrap">
          {LIBRARY_VIEWS.map((v) => (
            <ToggleGroupItem
              key={v}
              value={v}
              className="h-9 gap-2 rounded-full border bg-card px-3.5 font-heading text-[0.95rem] text-muted-foreground aria-pressed:border-foreground aria-pressed:bg-foreground aria-pressed:text-background"
            >
              {VIEWS[v][0]}
              <b className="font-bold text-foreground tabular-nums group-aria-pressed/toggle:text-primary">{String(clips.filter(VIEWS[v][1]).length)}</b>
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <div className="flex flex-wrap items-center gap-2 lg:ml-auto">
          <div className="relative min-w-0 flex-[1_1_14rem] lg:w-64 lg:flex-none">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="q"
              type="search"
              placeholder="Search titles, captions, streamers"
              aria-label="Search clips"
              className="h-9 rounded-full bg-card pl-9"
              value={q}
              onChange={(e) => set({ q: e.target.value })}
            />
          </div>
          <Select items={streamerItems} value={streamer || ALL} onValueChange={(v) => set({ streamer: !v || v === ALL ? "" : String(v) })}>
            <SelectTrigger aria-label="Streamer" className="h-9! min-w-36 rounded-full bg-card px-3.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {streamerItems.map((i) => (
                <SelectItem key={i.value} value={i.value}>
                  {i.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            items={Object.entries(SORT_LABELS).map(([value, label]) => ({ value, label }))}
            value={sort}
            onValueChange={(v) => v && set({ sort: v as Sort })}
          >
            <SelectTrigger aria-label="Sort" className="h-9! min-w-36 rounded-full bg-card px-3.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(SORT_LABELS).map(([v, name]) => (
                <SelectItem key={v} value={v}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="mt-5 flex min-h-8 items-center gap-3">
        <span id="count" className="text-sm text-muted-foreground tabular-nums">
          {rows.length === clips.length ? `${clips.length} clips` : `${rows.length} of ${clips.length} clips`}
        </span>
        {view === "skip" && rows.length > 0 && <DeleteSkipped />}
      </div>
      <ul id="list" aria-label="Clips" className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 2xl:grid-cols-6">
        {rows.map((c) => (
          <li key={c.id}>
            <Tile clip={c} dim={view === "all" && c.verdict === "skip"} />
          </li>
        ))}
      </ul>
      {!rows.length && loaded && (
        <div className="py-20 text-center">
          <p className="font-heading text-2xl font-bold">{clips.length ? "No clips match" : "No clips yet"}</p>
          <p className="mt-2 text-muted-foreground">
            {clips.length ? "Try another filter or search." : "Run clipit on a VOD, or clipit --poll, and its clips show up here."}
          </p>
        </div>
      )}
    </>
  );
}

const VERDICT: Record<string, [string, string]> = { keep: ["Kept", "bg-keep"], skip: ["Skipped", "bg-skip"], new: ["New", "bg-primary"] };

// glass labels sit on the video: they stay readable whatever frame is underneath
const glass = "rounded-md bg-black/55 px-1.5 py-0.5 text-[0.7rem] leading-4 font-semibold text-white backdrop-blur-md";

function Tile({ clip: c, dim }: { clip: Clip; dim: boolean }) {
  const [verdict, dot] = VERDICT[c.verdict ?? "new"];
  return (
    <button
      type="button"
      onClick={() => openClip(c.id)}
      aria-label={c.title || "Untitled clip"}
      className={`group relative block w-full overflow-hidden rounded-2xl text-left shadow-sm transition-[transform,opacity] duration-150 ease-out active:scale-[0.98] ${dim ? "opacity-55 hover:opacity-100" : ""}`}
    >
      <Frame clip={c} preview />
      <span className="pointer-events-none absolute inset-0 rounded-2xl ring-1 ring-black/5 ring-inset dark:ring-white/8" />
      <span className={`absolute top-2 left-2 font-heading text-sm! ${glass} ${hot(c) ? "text-primary!" : ""}`}>{score(c.score)}</span>
      <span className="absolute top-2 right-2 flex flex-col items-end gap-1">
        <span className={`flex items-center gap-1.5 ${glass}`}>
          <span className={`size-1.5 rounded-full ${dot}`} aria-hidden="true" />
          {verdict}
        </span>
        {(Object.keys(PLATFORMS) as Platform[])
          .filter((p) => c.uploads[p])
          .map((p) => (
            <span key={p} className={`flex items-center gap-1 ${glass}`}>
              <Check className="size-3 text-keep" strokeWidth={3} />
              {PLATFORMS[p]}
            </span>
          ))}
      </span>
      <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-linear-to-t from-black/85 via-black/45 to-transparent px-3 pt-16 pb-3 text-white">
        <span className="line-clamp-2 font-heading text-[0.95rem] leading-tight font-semibold">{c.title || "(untitled)"}</span>
        <span className="@container mt-1 flex gap-2.5 text-xs whitespace-nowrap text-white/70 tabular-nums">
          <span className="truncate">{c.streamer || "unknown"}</span>
          <span className="shrink-0">{secs(c)}</span>
          {/* a narrow tile (two columns on a phone) drops the date before it squeezes the rest */}
          <span className="ml-auto hidden shrink-0 @[12rem]:inline">{day(c.created_at)}</span>
        </span>
      </span>
    </button>
  );
}

function DeleteSkipped() {
  const [asking, setAsking] = useState(false);
  const skipped = useClipit(useShallow((s) => s.data.clips.filter(VIEWS.skip[1]).map((c) => c.id)));
  const purge = async () => {
    flushSync(() => release(skipped)); // every player lets go of these videos first: Windows can't delete one being read
    set((s) => ({ sheet: s.sheet !== null && skipped.includes(s.sheet) ? null : s.sheet }));
    const failed = await deleteSkipped();
    if (failed) {
      toast.error(`${failed} clip${failed === 1 ? "" : "s"} weren't deleted: the video is open in another program. Close it and try again.`, {
        duration: Infinity,
        closeButton: true,
      });
    }
  };
  return (
    <>
      <Button id="purge" type="button" variant="destructive" size="sm" className="ml-auto h-8 rounded-full px-3" onClick={() => setAsking(true)}>
        <Trash2 data-icon="inline-start" />
        Delete all skipped
      </Button>
      <Confirm
        open={asking}
        onOpenChange={setAsking}
        title={skipped.length === 1 ? "Delete the skipped clip?" : `Delete all ${skipped.length} skipped clips?`}
        body={DELETE_NOTE}
        action={skipped.length === 1 ? "Delete it" : `Delete ${skipped.length} clips`}
        destructive
        onConfirm={purge}
      />
    </>
  );
}
