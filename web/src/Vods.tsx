// The VODs tab: every stream clipit picked up, how it went, and why the failed ones failed.
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { deleteVod, useClipit, type Vod } from "./store";
import { Confirm, link, when } from "./ui";

const STATUS_STYLE: Record<string, string> = {
  done: "bg-keep-bg text-keep",
  failed: "bg-skip-bg text-skip",
  processing: "bg-warn-bg text-warn",
};

export function Vods() {
  const { vods, clips } = useClipit((s) => s.data);
  const loaded = useClipit((s) => s.loaded);
  const count: Record<string, number> = {};
  for (const c of clips) count[c.vod_id] = (count[c.vod_id] || 0) + 1;
  return (
    <div className="overflow-hidden rounded-2xl border bg-card">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            {["VOD", "Streamer", "Status", "Clips", "Tier", "Finished", ""].map((h) => (
              <TableHead key={h || "delete"} className={`h-11 font-heading text-muted-foreground first:pl-5 ${h === "Clips" ? "text-right" : ""}`}>
                {h}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody id="vods">
          {vods.map((v) => (
            <TableRow key={v.id} className="align-top">
              <TableCell className="py-3 pl-5 font-medium">{link(v.url, v.id)}</TableCell>
              <TableCell className="py-3 font-semibold">{v.streamer || "–"}</TableCell>
              <TableCell className="max-w-md py-3 whitespace-normal">
                <span className="flex items-center gap-2">
                  <Badge className={`font-heading ${STATUS_STYLE[v.status] ?? "bg-muted text-muted-foreground"}`}>{v.status}</Badge>
                  {v.attempts > 1 && <span className="text-muted-foreground">{`try ${v.attempts}`}</span>}
                </span>
                {v.error && <p className="mt-1.5 text-sm break-words text-skip">{v.error}</p>}
              </TableCell>
              <TableCell className="py-3 text-right font-heading text-base tabular-nums">{String(count[v.id] || 0)}</TableCell>
              <TableCell className="py-3 text-muted-foreground">{v.tier}</TableCell>
              <TableCell className="py-3 text-muted-foreground tabular-nums">{when(v.finished_at || v.claimed_at)}</TableCell>
              <TableCell className="py-2 pr-4 text-right">
                <DeleteVod vod={v} />
              </TableCell>
            </TableRow>
          ))}
          {loaded && !vods.length && (
            <TableRow>
              <TableCell colSpan={7} className="py-16 text-center text-muted-foreground">
                No VODs yet. Run clipit on a VOD, or clipit --poll.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}

// Not while it's being processed: the server refuses that too.
function DeleteVod({ vod: v }: { vod: Vod }) {
  const [asking, setAsking] = useState(false);
  if (v.status === "processing") return null;
  return (
    <>
      <Button type="button" variant="ghost" size="icon-sm" aria-label={`Delete VOD ${v.id}`} onClick={() => setAsking(true)}>
        <Trash2 />
      </Button>
      <Confirm
        open={asking}
        onOpenChange={setAsking}
        title="Delete this VOD?"
        body="Its chat, transcripts, audio and everything else clipit saved for it are deleted, and polling won't clip it again. Its clips and their videos stay. This can't be undone."
        action="Delete VOD"
        destructive
        onConfirm={() => void deleteVod(v.id)}
      />
    </>
  );
}
