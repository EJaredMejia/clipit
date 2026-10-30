// The Run tab: start clipit --poll, then --upload, from here, with the settings saved below, and watch each step and
// its output as it goes. Re-edits started from a clip run here too. The run goes on when the dashboard is closed, and
// shows here again when it's reopened.
import { Check, Circle, CircleSlash, Loader2, Play, Square, X } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { isReedit, openClip, type Run as RunState, runResult, type Settings, type Step, saveSettings, startRun, stopRun, useClipit } from "./store";
import { Confirm, Meta, when } from "./ui";

const STEP_ICON: Record<Step["status"], React.ReactNode> = {
  waiting: <Circle className="size-5 text-muted-foreground/50" />,
  running: <Loader2 className="size-5 animate-spin text-warn motion-reduce:animate-none" />,
  done: <Check className="size-5 text-keep" />,
  failed: <X className="size-5 text-skip" />,
  stopped: <Square className="size-4 text-muted-foreground" />,
  interrupted: <CircleSlash className="size-5 text-skip" />,
};

export function Run() {
  const run = useClipit((s) => s.run);
  const made = useClipit(useShallow((s) => (s.run && !isReedit(s.run.job) ? runResult(s.run, s.data.clips) : null)));
  const [asking, setAsking] = useState(false);
  const [starting, setStarting] = useState(false);
  const running = !!run?.running;
  const uploading = !!run?.steps.some((s) => s.name === "Post to YouTube" && s.status === "running");

  const start = async () => {
    setStarting(true);
    await startRun();
    setStarting(false);
  };

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-xl">
          <h2 className="font-heading text-3xl font-bold">Run clipit</h2>
          <p className="mt-2 text-muted-foreground">
            Clips the latest VODs of trending and big streamers, then posts the next due clip to YouTube Shorts, at most 10 a
            day and an hour apart. The same as clipit --poll, then clipit --upload, set up below. You get a notice here
            when it ends.
          </p>
        </div>
        {running ? (
          <Button
            type="button"
            variant="outline"
            className="h-12 rounded-xl px-6 font-heading text-base font-bold"
            disabled={run.stopping}
            onClick={() => setAsking(true)}
          >
            {run.stopping ? <Loader2 className="animate-spin motion-reduce:animate-none" /> : <Square />}
            {run.stopping ? "Stopping…" : "Stop"}
          </Button>
        ) : (
          <Button type="button" className="h-12 rounded-xl px-6 font-heading text-base font-bold" disabled={starting} onClick={() => void start()}>
            <Play />
            Run now
          </Button>
        )}
      </div>

      {run && run.id > 0 ? (
        <section aria-label="Latest run" className="rounded-2xl border bg-card p-5">
          <Meta
            parts={[
              <b className="font-semibold text-foreground">{run.stopping ? "Stopping" : running ? "Running" : "Last run"}</b>,
              ...jobParts(run.job),
              `started ${when(run.started)}`,
              ...(run.ended ? [`ended ${when(run.ended)}`] : []),
              ...(made ? [`${made.clips} new clip${made.clips === 1 ? "" : "s"}`, `${made.posted} posted to YouTube`] : []),
            ]}
          />
          <ol className="mt-4 flex flex-col gap-2">
            {run.steps.map((s, i) => (
              <li key={s.name} className="flex items-center gap-3">
                <span className="flex size-6 items-center justify-center" aria-hidden="true">
                  {STEP_ICON[s.status]}
                </span>
                <span className="font-heading font-semibold">{`${i + 1}. ${s.name}`}</span>
                <span className={`text-sm ${s.status === "failed" ? "text-skip" : "text-muted-foreground"}`}>{s.status}</span>
              </li>
            ))}
          </ol>
          {running && <Doing doing={run.doing} />}
          {run.stopping && uploading && (
            <p className="mt-3 text-sm text-warn">Stopping once the YouTube post finishes, so it's never cut off half sent.</p>
          )}
          {run.steps.some((s) => s.status === "interrupted") && (
            <p className="mt-3 text-sm text-skip">
              The run stopped without finishing: clipit was closed or the PC restarted. A VOD it was clipping is retried next run.
            </p>
          )}
          <Output lines={run.lines} />
        </section>
      ) : (
        run && <p className="text-muted-foreground">No runs yet.</p>
      )}

      {run && <RunSettings saved={run.settings} running={running} />}

      <Confirm
        open={asking}
        onOpenChange={setAsking}
        title="Stop the run?"
        body={
          uploading
            ? "The YouTube post going now finishes first, so it's never cut off half sent. Then the run stops."
            : "Clipping stops now, with everything it started, and posting to YouTube is skipped. A VOD it was clipping is retried next run."
        }
        action="Stop the run"
        destructive
        onConfirm={() => void stopRun()}
      />
    </div>
  );
}

// What the run was asked to do: the clip it re-edits, or what it clips differently from the defaults.
function jobParts(job: RunState["job"]): React.ReactNode[] {
  if (isReedit(job)) {
    return [
      <button type="button" className="underline-offset-2 hover:underline" onClick={() => openClip(job.reedit)}>
        {`Re-edit of clip ${job.reedit}`}
      </button>,
      `“${job.request}”`,
    ];
  }
  if (!job) return [];
  return [...(job.high_effort ? ["high effort"] : []), ...(job.upload ? [] : ["no posting"])];
}

type Form = Omit<Settings, "top" | "clips" | "prioritize"> & { top: string; clips: string; prioritize: string };
const asForm = (s: Settings): Form => ({ ...s, top: String(s.top), clips: String(s.clips), prioritize: s.prioritize.join("\n") });
const fieldLabel = "font-heading text-[0.95rem] font-semibold";

// How the next runs go, saved on the server: the flags of clipit --poll and --upload. The server checks them.
function RunSettings({ saved, running }: { saved: Settings; running: boolean }) {
  const [form, setForm] = useState<Form | null>(null); // null: showing what's saved
  const [saving, setSaving] = useState(false);
  const f = form ?? asForm(saved);
  const settings: Settings = {
    ...f,
    top: Number(f.top),
    clips: Number(f.clips),
    lang: f.lang.trim(),
    prioritize: f.prioritize.split(/[\s,]+/).filter(Boolean),
  };
  const changed = form !== null && JSON.stringify(settings) !== JSON.stringify(saved);
  const edit = (patch: Partial<Form>) => setForm({ ...f, ...patch });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    if (await saveSettings(settings)) setForm(null);
    setSaving(false);
  };
  const number = (key: "top" | "clips", name: string, hint: string) => (
    <label className="flex flex-col gap-1.5">
      <span className={fieldLabel}>{name}</span>
      <Input type="number" required min={1} max={20} step={1} className="h-10 bg-card" value={f[key]} onChange={(e) => edit({ [key]: e.target.value })} />
      <span className="text-xs text-muted-foreground">{hint}</span>
    </label>
  );
  const check = (key: "high_effort" | "upload", name: string, hint: string) => (
    <label className="flex items-start gap-3">
      <input type="checkbox" className="mt-1 size-4 accent-primary" checked={f[key]} onChange={(e) => edit({ [key]: e.target.checked })} />
      <span>
        <span className={fieldLabel}>{name}</span>
        <span className="block text-sm text-muted-foreground">{hint}</span>
      </span>
    </label>
  );
  return (
    <form aria-label="Run settings" className="rounded-2xl border bg-card p-5" onSubmit={(e) => void submit(e)}>
      <h3 className="font-heading text-lg font-bold">Run settings</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        {running ? "The next run uses these. The run going now keeps the ones it started with." : "The next run uses these."}
      </p>
      <div className="mt-4 grid gap-4 sm:grid-cols-3">
        {number("top", "Streamers to clip", "Trending first, then the biggest live")}
        {number("clips", "Max clips per VOD", "The AI picks up to this many, fewer or none if the VOD is weak")}
        <label className="flex flex-col gap-1.5">
          <span className={fieldLabel}>Language</span>
          <Input className="h-10 bg-card" maxLength={3} pattern="[A-Za-z]{0,3}" value={f.lang} onChange={(e) => edit({ lang: e.target.value })} />
          <span className="text-xs text-muted-foreground">Stream language code, like EN. Empty for any</span>
        </label>
      </div>
      <label className="mt-4 flex flex-col gap-1.5">
        <span className={fieldLabel}>Streamers to clip and post first</span>
        <Textarea
          rows={2}
          placeholder="twitch:caedrel"
          className="min-h-0 resize-y bg-card font-mono text-sm"
          value={f.prioritize}
          onChange={(e) => edit({ prioritize: e.target.value })}
        />
        <span className="text-xs text-muted-foreground">One per line, twitch:name or kick:name, while an event they cover is big</span>
      </label>
      <div className="mt-4 flex flex-col gap-3">
        {check("high_effort", "High-effort edits", "The AI also edits every clip it scored high enough to post (cuts, zooms, sounds, pop-up text, framing), looks at frames of it and reviews it until it's really good. Much slower")}
        {check("upload", "Post to YouTube after clipping", "Off: the run only clips, nothing goes up")}
      </div>
      <div className="mt-5 flex flex-wrap gap-2">
        <Button type="submit" disabled={!changed || saving}>
          {saving ? <Loader2 className="animate-spin motion-reduce:animate-none" /> : <Check />}
          Save settings
        </Button>
        {form && (
          <Button type="button" variant="ghost" onClick={() => setForm(null)}>
            Undo changes
          </Button>
        )}
      </div>
    </form>
  );
}

// What the run is doing right now, as clipit says it, above the raw output.
function Doing({ doing }: { doing: RunState["doing"] }) {
  if (!doing?.what) return null; // none before a step says something
  const [done, of] = doing.count ?? [0, 0];
  return (
    <div role="status" className="mt-4 rounded-xl border border-warn/30 bg-warn/5 p-4">
      <p className="flex flex-wrap items-baseline gap-x-2">
        <b className="font-heading text-lg font-bold">{doing.what}</b>
        {doing.vod && <span className="text-sm text-muted-foreground">{`VOD ${doing.vod[0]} of ${doing.vod[1]}`}</span>}
      </p>
      {doing.stage && (
        <p className="mt-1 flex items-center gap-2 text-sm">
          <Loader2 className="size-4 shrink-0 animate-spin text-warn motion-reduce:animate-none" aria-hidden="true" />
          <span className="truncate">{doing.stage}</span>
          {of > 0 && <span className="ml-auto shrink-0 text-muted-foreground tabular-nums">{`${done}/${of}`}</span>}
        </p>
      )}
      {of > 0 && (
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div
            className="h-full origin-left rounded-full bg-warn transition-transform duration-200 ease-out motion-reduce:transition-none"
            style={{ transform: `scaleX(${Math.min(1, done / of)})` }}
          />
        </div>
      )}
    </div>
  );
}

// clipit's output, following the newest line unless you've scrolled up to read.
function Output({ lines }: { lines: string[] }) {
  const box = useRef<HTMLPreElement>(null);
  const follow = useRef(true);
  const shown = useClipit((s) => s.tab === "run"); // a hidden tab has no height to scroll: catch up when it's shown
  useEffect(() => {
    if (shown && follow.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, shown]);
  return (
    <pre
      ref={box}
      tabIndex={0}
      aria-label="Run output"
      onScroll={(e) => {
        const el = e.currentTarget;
        follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
      className="mt-4 max-h-[50vh] min-h-32 overflow-auto rounded-xl bg-muted/60 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words"
    >
      {lines.length ? lines.join("\n") : "Waiting for output…"}
    </pre>
  );
}
