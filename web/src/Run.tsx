// The Run tab: start clipit --poll, then --upload, from here, and watch each step and its output as it goes.
import { Check, Circle, Loader2, Play, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { Button } from "@/components/ui/button";
import { runResult, type Step, startRun, stopRun, useClipit } from "./store";
import { Confirm, Meta, when } from "./ui";

const STEP_ICON: Record<Step["status"], React.ReactNode> = {
  waiting: <Circle className="size-5 text-muted-foreground/50" />,
  running: <Loader2 className="size-5 animate-spin text-warn motion-reduce:animate-none" />,
  done: <Check className="size-5 text-keep" />,
  failed: <X className="size-5 text-skip" />,
  stopped: <Square className="size-4 text-muted-foreground" />,
};

export function Run() {
  const run = useClipit((s) => s.run);
  const made = useClipit(useShallow((s) => (s.run ? runResult(s.run, s.data.clips) : null)));
  const [asking, setAsking] = useState(false);
  const [starting, setStarting] = useState(false);
  const running = !!run?.running;

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
            day and an hour apart. The same as clipit --poll, then clipit --upload. You get a notice here when it ends.
          </p>
        </div>
        {running ? (
          <Button type="button" variant="outline" className="h-12 rounded-xl px-6 font-heading text-base font-bold" onClick={() => setAsking(true)}>
            <Square />
            Stop
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
              <b className="font-semibold text-foreground">{running ? "Running" : "Last run"}</b>,
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
          <Output lines={run.lines} />
        </section>
      ) : (
        run && <p className="text-muted-foreground">No run since the dashboard started.</p>
      )}

      <Confirm
        open={asking}
        onOpenChange={setAsking}
        title="Stop the run?"
        body="The step that's running is killed and the rest are skipped. A VOD it was clipping is retried next run. A clip it was posting may already be on YouTube: check YouTube Studio, then add its link in the clip."
        action="Stop the run"
        destructive
        onConfirm={() => void stopRun()}
      />
    </div>
  );
}

// clipit's output, following the newest line unless you've scrolled up to read.
function Output({ lines }: { lines: string[] }) {
  const box = useRef<HTMLPreElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    if (follow.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);
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
