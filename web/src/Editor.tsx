// One clip, ready to post: its video, the text for each platform, and the steps to put it up. The Post screen and the
// Library's side sheet both show it.
import { Check, CheckCircle2, ChevronRight, Copy, Crosshair, Loader2, Sparkles, Trash2, Upload, X } from "lucide-react";
import { type FormEvent, Fragment, type ReactNode, type RefObject, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  type Clip,
  DELETE_NOTE,
  deleteClip,
  description,
  flushTexts,
  isReedit,
  load,
  markPosted,
  openClip,
  type Platform,
  PLATFORMS,
  postYoutube,
  release,
  save,
  saveText,
  saveVerdict,
  setDraft,
  showTab,
  startReedit,
  useClipit,
  type Verdict,
} from "./store";
import { ClipFacts, Confirm, FileTools, link, media, Posted, safeUrl, when } from "./ui";

const label = "font-heading text-[0.95rem] font-semibold";

export function ClipEditor({ clip: c, layout }: { clip: Clip; layout: "page" | "sheet" }) {
  const released = useClipit((s) => s.released.includes(c.id));
  const player = useRef<HTMLVideoElement>(null);
  const src = media(c);
  const video = !released && (
    <video ref={player} key={src} src={src} controls preload="metadata" playsInline className="size-full object-contain" />
  );
  // Laid out by the room it's given, not the window's width: the same editor sits in a narrow sheet or a wide page.
  // Below 42rem the video stacks above the text; above it, the video stays put beside the text while you scroll.
  if (layout === "sheet") {
    // The video is the sheet's left half: a black pane its full height, the way a phone shows it. Only the text scrolls.
    return (
      <div className="@container h-full">
        <div className="h-full overflow-y-auto @2xl:grid @2xl:grid-cols-[auto_minmax(0,1fr)] @2xl:overflow-hidden">
          <div className="flex justify-center bg-black @2xl:h-full @2xl:w-[min(45cqi,calc(100dvh*9/16))]">
            <div className="aspect-[9/16] h-[min(62dvh,36rem)] @2xl:h-full @2xl:w-full">{video}</div>
          </div>
          <div className="p-5 pt-6 sm:p-8 @2xl:overflow-y-auto">
            <ClipText clip={c} page={false} player={player} />
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="@container">
      <div className="grid gap-8 @2xl:grid-cols-[min(40cqi,calc((100dvh-8rem)*9/16))_minmax(0,1fr)] @2xl:items-start @5xl:gap-12">
        <div className="@2xl:sticky @2xl:top-24">
          <div className="mx-auto aspect-[9/16] h-[min(70dvh,40rem)] overflow-hidden rounded-2xl bg-black shadow-(--shadow-card) @2xl:h-auto @2xl:w-full">
            {video}
          </div>
        </div>
        <ClipText clip={c} page player={player} />
      </div>
    </div>
  );
}

function ClipText({ clip: c, page, player }: { clip: Clip; page: boolean; player: RefObject<HTMLVideoElement | null> }) {
  return (
    <div className="min-w-0">
      <h2
        className={`font-heading leading-[1.08] font-bold text-balance break-words ${page ? "text-[clamp(1.75rem,4.2cqi,2.4rem)]" : "pr-10 text-[clamp(1.5rem,4cqi,1.85rem)]"}`}
      >
        {c.title || "(untitled)"}
      </h2>
      <ClipFacts clip={c} className="mt-3" />
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <FileTools clip={c} />
        <DeleteButton clip={c} />
      </div>
      <p className="mt-2 text-xs break-all text-muted-foreground/80">{c.file}</p>

      {/* one block per platform, in the order you post: copy the text, upload the file, mark it posted */}
      <PlatformBlock clip={c} platform="tiktok" title="TikTok">
        <Editable clip={c} field="caption" label="Caption" text={c.caption} max={2200} platform="tiktok" />
        <UploadBox clip={c} platform="tiktok" primary />
      </PlatformBlock>
      <PlatformBlock clip={c} platform="youtube" title="YouTube Shorts">
        <Editable clip={c} field="title" label="Title" text={c.title} max={100} platform="youtube" />
        <Editable clip={c} field="youtube" label="Description" text={description(c)} max={5000} platform="youtube" />
        <PostNow clip={c} />
        <UploadBox clip={c} platform="youtube" primary={false} />
      </PlatformBlock>

      <Reedit clip={c} player={player} />

      <div className="mt-8 flex flex-wrap items-center gap-3 border-t pt-6" role="group" aria-label="Worth posting?">
        <span className={`${label} mr-auto text-muted-foreground`}>Worth posting?</span>
        <ToggleGroup
          variant="outline"
          value={c.verdict ? [c.verdict] : []}
          onValueChange={(v) => saveVerdict(c.id, (v[0] as Verdict | undefined) ?? null)}
        >
          <ToggleGroupItem value="keep" className={`${label} h-9 px-3.5 aria-pressed:border-keep aria-pressed:bg-keep-bg aria-pressed:text-keep`}>
            <Check />
            Keep
          </ToggleGroupItem>
          <ToggleGroupItem value="skip" className={`${label} h-9 px-3.5 aria-pressed:border-skip aria-pressed:bg-skip-bg aria-pressed:text-skip`}>
            <X />
            Skip
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      <Notes clip={c} />
    </div>
  );
}

function PlatformBlock({ clip: c, platform, title, children }: { clip: Clip; platform: Platform; title: string; children: ReactNode }) {
  return (
    <section className="mt-8" aria-label={title}>
      <div className="flex items-center justify-between gap-2 border-b pb-2.5">
        <h3 className="font-heading text-lg font-bold">{title}</h3>
        <Posted clip={c} platform={platform} />
      </div>
      <div className="mt-4 flex flex-col gap-4">{children}</div>
    </section>
  );
}

function Notes({ clip: c }: { clip: Clip }) {
  const notes = [["Hook", c.hook], ["Why", c.why], ["Trend", c.trend], ["Sound to add", c.sound]].filter(([, v]) => v);
  if (!notes.length) return null;
  return (
    <Collapsible className="mt-6">
      <CollapsibleTrigger className={`${label} group flex items-center gap-1.5 text-muted-foreground hover:text-foreground`}>
        <ChevronRight className="size-4 transition-transform duration-200 ease-out group-data-panel-open:rotate-90" />
        Why the AI picked it
      </CollapsibleTrigger>
      <CollapsibleContent className="transition-opacity duration-150 ease-out data-ending-style:opacity-0 data-starting-style:opacity-0">
        <dl className="grid grid-cols-[max-content_1fr] gap-x-5 gap-y-2 pt-3 text-sm">
          {notes.map(([k, v]) => (
            <Fragment key={k}>
              <dt className="text-muted-foreground">{k}</dt>
              <dd>{v}</dd>
            </Fragment>
          ))}
        </dl>
      </CollapsibleContent>
    </Collapsible>
  );
}

function UploadBox({ clip: c, platform, primary }: { clip: Clip; platform: Platform; primary: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const key = `${c.id}:${platform}`;
  const draft = useClipit((s) => s.drafts[key]);
  const name = PLATFORMS[platform];
  const done = c.uploads[platform];
  const undo = (
    <Button type="button" variant="ghost" size="xs" className="ml-auto text-current" onClick={() => save("/api/upload", { clip: c.id, platform, url: null })}>
      Undo
    </Button>
  );
  if (done && done.status === "uploading") {
    // an upload this old belongs to a run that died (youtube.STUCK): the user has to check whether it went up
    const stuck = Date.now() - Date.parse(done.at) > 2 * 3600e3;
    return (
      <div className="flex items-center gap-2.5 rounded-xl bg-warn-bg px-3.5 py-3 text-sm font-medium text-warn" role="status">
        {stuck ? (
          <>
            <span>{`The upload to ${name} that started ${when(done.at)} never finished. Check YouTube Studio: undo to post it again.`}</span>
            {undo}
          </>
        ) : (
          <>
            <Loader2 className="size-4 shrink-0 animate-spin" />
            <span>{`Uploading to ${name}, started ${when(done.at)}. It keeps going if you leave this page, as long as the dashboard runs.`}</span>
          </>
        )}
      </div>
    );
  }
  if (done) {
    return (
      <div className="flex items-center gap-2.5 rounded-xl bg-keep-bg px-3.5 py-2.5 text-sm font-medium text-keep">
        <CheckCircle2 className="size-4 shrink-0" />
        <span>
          {`Posted on ${name}, ${when(done.at)}`}
          {done.url && <>. {link(done.url, "Open the post")}</>}
        </span>
        {undo}
      </div>
    );
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const box = input.current!;
    const url = box.value.trim();
    if (url && !safeUrl(url)) {
      box.setCustomValidity("Use a full https:// link");
      box.reportValidity();
      return;
    }
    void markPosted(c, platform, url);
  };
  return (
    <form className="flex flex-wrap gap-2" onSubmit={submit}>
      <Input
        ref={input}
        type="url"
        placeholder={`${name} link (optional)`}
        aria-label={`${name} post link`}
        className="h-10 min-w-0 flex-[1_1_12rem] bg-card"
        value={draft || ""}
        onChange={(e) => {
          setDraft(key, e.target.value);
          e.target.setCustomValidity("");
        }}
      />
      <Button type="submit" variant={primary ? "default" : "outline"} className="h-10 px-4 font-heading text-[0.95rem] font-bold">
        <Check data-icon="inline-start" />
        {`Mark posted on ${name}`}
      </Button>
    </form>
  );
}

// Post text the user can edit and copy. Saved edits are theirs: a poll never overwrites a draft.
function Editable(props: { clip: Clip; field: string; label: string; text: string; max: number; platform: Platform }) {
  const { clip: c, field, label: name, text, max, platform } = props;
  const box = useRef<HTMLTextAreaElement>(null);
  const [copy, setCopy] = useState<"Copy" | "Copied" | "Press Ctrl+C">("Copy");
  const key = `${c.id}:text:${field}`; // post-link drafts use "id:platform"
  const value = useClipit((s) => s.drafts[key]) ?? text;
  const status = useClipit((s) => s.textStatus[key]) ?? "Saved automatically";
  const trouble = !status.startsWith("Sav");
  const copyText = async () => {
    try {
      await navigator.clipboard.writeText(box.current!.value);
      setCopy("Copied");
    } catch {
      box.current!.select(); // clipboard blocked (some embedded browsers): select it for Ctrl+C
      setCopy("Press Ctrl+C");
    }
    setTimeout(() => setCopy("Copy"), 2000);
  };
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={label}>{name}</span>
        <span className={`ml-auto text-xs tabular-nums ${value.length > max * 0.95 ? "text-warn" : "text-muted-foreground"}`}>{`${value.length}/${max}`}</span>
        <span className={`text-xs ${trouble ? "font-medium text-warn" : "text-muted-foreground"}`} role="status">
          {status}
        </span>
        <Button type="button" variant="outline" size="xs" className="min-w-[4.75rem]" onClick={copyText}>
          {copy === "Copied" ? <Check className="text-keep" /> : <Copy />}
          {copy}
        </Button>
      </div>
      <Textarea
        ref={box}
        rows={field === "title" ? 2 : field === "caption" ? 6 : 5}
        maxLength={max}
        aria-label={name}
        aria-invalid={trouble || undefined}
        className="min-h-0 resize-y bg-card leading-relaxed"
        value={value}
        onChange={(e) => {
          setDraft(key, e.target.value);
          void saveText(c.id, field);
        }}
        onBlur={() => void saveText(c.id, field)}
      />
      {c.uploads[platform]?.status === "done" && (
        <p className="mt-1.5 text-xs text-muted-foreground">{`Already posted: saving here doesn't change the ${PLATFORMS[platform]} post.`}</p>
      )}
    </div>
  );
}

// Posts the clip through the server (clipit --upload's code), after saving the latest title and description.
function PostNow({ clip: c }: { clip: Clip }) {
  const [step, setStep] = useState<"idle" | "saving" | "confirm" | "posting">("idle");
  if (c.uploads.youtube) return null;
  const start = async () => {
    setStep("saving");
    if (!(await flushTexts(c.id, ["title", "youtube"]))) {
      toast.error("Couldn't save your latest title or description. Check the fields before posting.");
      return setStep("idle");
    }
    setStep("confirm");
  };
  const post = async () => {
    setStep("posting");
    setTimeout(load, 1000); // show the uploading state now, not at the next poll
    try {
      const { url } = await postYoutube(c.id);
      toast.success("Posted on YouTube", { description: link(typeof url === "string" ? url : null, "Open the post") });
    } catch (e) {
      // stays up until closed: the header's status light would be cleared by the next poll
      toast.error(`Couldn't post to YouTube: ${(e as Error).message}`, { duration: Infinity, closeButton: true });
    }
    setStep("idle");
    await load();
  };
  return (
    <>
      <Button
        type="button"
        className="h-11 self-start rounded-xl px-5 font-heading text-base font-bold"
        disabled={step !== "idle" && step !== "confirm"}
        onClick={start}
      >
        {step === "saving" || step === "posting" ? <Loader2 className="animate-spin" /> : <Upload />}
        {step === "posting" ? "Posting…" : step === "saving" ? "Saving…" : "Post to YouTube now"}
      </Button>
      <Confirm
        open={step === "confirm"}
        // closing through Post leaves step at "posting"; only Cancel or Esc go back to idle
        onOpenChange={(open) => !open && setStep((s) => (s === "confirm" ? "idle" : s))}
        title="Post this clip on YouTube now?"
        body="It goes up publicly on the channel you signed in to with clipit --youtube-login, with the title and description above."
        action="Post to YouTube"
        onConfirm={post}
      />
    </>
  );
}

const clock = (t: number) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;

// Ask the AI to edit the clip again: what to change, and the second of the video you mean (it only reads the speech
// and chat, so it can't see what happens on screen). It runs as a run, in the Run tab. The server refuses the same.
function Reedit({ clip: c, player }: { clip: Clip; player: RefObject<HTMLVideoElement | null> }) {
  const key = `${c.id}:reedit`;
  const text = useClipit((s) => s.drafts[key]) ?? "";
  const [at, setAt] = useState<number | null>(null);
  const [sending, setSending] = useState(false);
  const run = useClipit((s) => s.run);
  const vodGone = useClipit((s) => s.loaded && !s.data.vods.some((v) => v.id === c.vod_id));
  const going = !!run?.running && isReedit(run.job) && run.job.reedit === c.id;
  const blocked = c.uploads.youtube
    ? "It's on YouTube already, and a re-edit can't change a posted video."
    : vodGone
      ? "Its VOD's files were deleted, so it can't be re-edited."
      : run?.running && !going
        ? "A run is going. You can re-edit once it ends."
        : "";
  const send = async (e: FormEvent) => {
    e.preventDefault();
    setSending(true);
    if (await startReedit(c.id, text.trim(), at)) {
      setDraft(key, "");
      setAt(null);
    }
    setSending(false);
  };
  return (
    <section className="mt-8" aria-label="Re-edit with AI">
      <div className="border-b pb-2.5">
        <h3 className="font-heading text-lg font-bold">Re-edit with AI</h3>
      </div>
      {going ? (
        <div className="mt-4 flex items-center gap-2.5 rounded-xl bg-warn-bg px-3.5 py-3 text-sm font-medium text-warn" role="status">
          <Loader2 className="size-4 shrink-0 animate-spin motion-reduce:animate-none" />
          <span>{`Re-editing: “${run.job && isReedit(run.job) ? run.job.request : ""}”. The clip gets its new video when it's done.`}</span>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="ml-auto text-current"
            onClick={() => {
              openClip(null); // the sheet would cover the Run tab
              showTab("run");
            }}
          >
            See the run
          </Button>
        </div>
      ) : (
        <form className="mt-4 flex flex-col gap-2" onSubmit={(e) => void send(e)}>
          <Textarea
            rows={3}
            maxLength={500}
            required
            aria-label="What to change"
            placeholder="Like: a boom when he slams the desk, and zoom in on his face"
            className="min-h-0 resize-y bg-card leading-relaxed"
            disabled={!!blocked}
            value={text}
            onChange={(e) => setDraft(key, e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            The AI hears the speech and reads the chat, but can't see the video: pause on the moment you mean and mark it.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {at === null ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!!blocked}
                onClick={() => player.current && setAt(Math.round(player.current.currentTime * 10) / 10)}
              >
                <Crosshair data-icon="inline-start" />
                Mark this moment
              </Button>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-sm tabular-nums">
                {`At ${clock(at)}`}
                <Button type="button" variant="ghost" size="icon-xs" aria-label="Unmark the moment" onClick={() => setAt(null)}>
                  <X />
                </Button>
              </span>
            )}
            <Button type="submit" size="sm" className="ml-auto" disabled={!!blocked || sending || !text.trim()}>
              {sending ? <Loader2 className="animate-spin" /> : <Sparkles data-icon="inline-start" />}
              Re-edit
            </Button>
          </div>
          {blocked && <p className="text-sm text-muted-foreground">{blocked}</p>}
        </form>
      )}
      {c.reedits.length > 0 && (
        <ol className="mt-4 flex flex-col gap-2 text-sm" aria-label="Re-edits asked for">
          {[...c.reedits].reverse().map((r) => (
            <li key={r.created_at + r.request} className="rounded-lg bg-muted/50 px-3 py-2">
              <p className="break-words">
                {`“${r.request}”`}
                {r.at_s !== null && <span className="text-muted-foreground">{` at ${clock(r.at_s)}`}</span>}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {when(r.created_at)}
                {" · "}
                {r.error ? (
                  <span className="text-skip">{`Failed: ${r.error}`}</span>
                ) : r.result_id !== null && r.result_id !== c.id ? (
                  <button type="button" className="font-medium text-foreground underline-offset-2 hover:underline" onClick={() => openClip(r.result_id)}>
                    Open the edited clip
                  </button>
                ) : (
                  "Done"
                )}
              </p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

// Not while it's being uploaded: the server refuses that too.
function DeleteButton({ clip: c }: { clip: Clip }) {
  const [asking, setAsking] = useState(false);
  if (Object.values(c.uploads).some((u) => u.status === "uploading")) return null;
  const remove = () => {
    flushSync(() => release([c.id])); // every player lets go of the file now: Windows can't delete a video being read
    void deleteClip(c.id);
  };
  return (
    <>
      <Button type="button" variant="outline" size="sm" onClick={() => setAsking(true)}>
        <Trash2 data-icon="inline-start" />
        Delete
      </Button>
      <Confirm open={asking} onOpenChange={setAsking} title="Delete this clip?" body={DELETE_NOTE} action="Delete clip" destructive onConfirm={remove} />
    </>
  );
}
