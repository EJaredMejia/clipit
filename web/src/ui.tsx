// Small pieces every screen uses: formatting, links, the meta line, status badges, video frames, confirm dialogs.
import { AlertDialog as AlertDialogPrimitive } from "@base-ui/react/alert-dialog";
import { Check, FolderOpen, Loader2 } from "lucide-react";
import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type Clip, type Platform, PLATFORMS, reveal, setStatus, useClipit } from "./store";

export const safeUrl = (u: string | null | undefined) => (/^https?:\/\/\S+$/i.test(u || "") ? u! : null);
export const link = (href: string | null | undefined, text: string): ReactNode =>
  safeUrl(href) ? (
    <a href={safeUrl(href)!} target="_blank" rel="noopener noreferrer" className="text-link underline-offset-2 hover:underline">
      {text}
    </a>
  ) : (
    text
  );
export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "–");
export const score = (s: number) => String(Number.isInteger(s) ? s : s.toFixed(1));
const two = (n: number) => String(n).padStart(2, "0");
export const day = (iso: string) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
export const secs = (c: Clip) => `${Math.round(c.end_s - c.start_s)} s`;
export const hms = (s: number) => `${Math.floor(s / 3600)}:${two(Math.floor(s / 60) % 60)}:${two(Math.floor(s) % 60)}`;
export const media = (c: Clip) => `/media/${c.id}?f=${encodeURIComponent(c.file)}`; // a new file gets a new URL, not the cached copy
export const hot = (c: Clip) => c.score >= 8;

export function atTime(url: string | null, s: number): string | null {
  try {
    const u = new URL(url ?? "");
    u.searchParams.set("t", `${Math.floor(s / 3600)}h${Math.floor(s / 60) % 60}m${Math.floor(s) % 60}s`);
    return u.href;
  } catch {
    return null;
  }
}

// a span, not a div: it sits inside buttons. Facts sit apart by spacing alone, no separators.
export const Meta = ({ parts, className = "" }: { parts: ReactNode[]; className?: string }) => (
  <span className={`flex flex-wrap items-baseline gap-x-3.5 gap-y-0.5 text-sm text-muted-foreground tabular-nums ${className}`}>
    {parts.map((p, i) => (typeof p === "string" ? <span key={i}>{p}</span> : <Fragment key={i}>{p}</Fragment>))}
  </span>
);

// The facts every clip header shows: who, where in the VOD, how long, how the AI rated it.
export const ClipFacts = ({ clip: c, className }: { clip: Clip; className?: string }) => (
  <Meta
    className={className}
    parts={[
      <b className="font-semibold text-foreground">
        {/^[\w-]+$/.test(c.streamer)
          ? link(`${c.vod_url?.startsWith("https://kick.com/") ? "https://kick.com" : "https://www.twitch.tv"}/${c.streamer}`, c.streamer)
          : c.streamer || "unknown streamer"}
      </b>,
      link(atTime(c.vod_url, c.start_s), `VOD at ${hms(c.start_s)}`),
      secs(c),
      <span className={hot(c) ? "font-semibold text-primary" : undefined}>{`AI score ${score(c.score)}/10`}</span>,
    ]}
  />
);

// Where a clip went up: a check once it's posted, a spinner while it uploads.
export function Posted({ clip: c, platform, className = "" }: { clip: Clip; platform: Platform; className?: string }) {
  const u = c.uploads[platform];
  if (!u) return null;
  return u.status === "uploading" ? (
    <Badge className={`bg-warn-bg font-heading text-warn ${className}`}>
      <Loader2 className="animate-spin" />
      {`Uploading to ${PLATFORMS[platform]}`}
    </Badge>
  ) : (
    <Badge className={`bg-keep-bg font-heading text-keep ${className}`}>
      <Check />
      {PLATFORMS[platform]}
    </Badge>
  );
}

// A clip's video, attached only while it's near the screen (a Library of hundreds of clips must not hold hundreds of
// videos) and let go of while the clip is being deleted. `at` shows that second as the still frame.
export function Frame({ clip: c, at = 1, preview = false, className = "" }: { clip: Clip; at?: number; preview?: boolean; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [near, setNear] = useState(false);
  const released = useClipit((s) => s.released.includes(c.id));
  useEffect(() => {
    const seen = new IntersectionObserver(([e]) => setNear(e.isIntersecting), { rootMargin: "300px" });
    seen.observe(box.current!);
    return () => seen.disconnect();
  }, []);
  // hovering a card plays it silently from the frame shown; leaving puts the frame back
  const play = (on: boolean) => {
    const v = video.current;
    if (!preview || !v) return;
    if (on) v.play().catch(() => {});
    else {
      v.pause();
      v.currentTime = at;
    }
  };
  return (
    <div
      ref={box}
      className={`relative aspect-[9/16] overflow-hidden bg-muted ${className}`}
      onPointerEnter={(e) => e.pointerType === "mouse" && play(true)}
      onPointerLeave={() => play(false)}
    >
      {near && !released && (
        <video ref={video} src={`${media(c)}#t=${at}`} muted loop playsInline preload="metadata" className="size-full object-cover" />
      )}
    </div>
  );
}

// Asks the server to open Explorer on the PC running clipit with the file selected, ready to drag into an upload page.
export function FileTools({ clip: c }: { clip: Clip }) {
  const [label, setLabel] = useState("Show in folder");
  const show = async () => {
    try {
      await reveal(c.id);
      setLabel("Opened");
    } catch (e) {
      setStatus(`Couldn't open the folder (${(e as Error).message})`, true);
    }
    setTimeout(() => setLabel("Show in folder"), 2000);
  };
  return (
    <Button type="button" variant="outline" size="sm" onClick={show}>
      <FolderOpen data-icon="inline-start" />
      {label}
    </Button>
  );
}

// A question before something that can't be taken back. The action button closes it, then acts.
export function Confirm(props: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  body: ReactNode;
  action: string;
  destructive?: boolean;
  onConfirm: () => void;
}) {
  return (
    <AlertDialog open={props.open} onOpenChange={props.onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="text-lg font-bold">{props.title}</AlertDialogTitle>
          <AlertDialogDescription>{props.body}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogPrimitive.Close render={<Button variant={props.destructive ? "destructive" : "default"} />} onClick={props.onConfirm}>
            {props.action}
          </AlertDialogPrimitive.Close>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
