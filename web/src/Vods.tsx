// The VODs tab: every stream clipit picked up, how it went, and why the failed ones failed.
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useClipit } from "./store";
import { link, when } from "./ui";

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
            {["VOD", "Streamer", "Status", "Clips", "Tier", "Finished"].map((h) => (
              <TableHead key={h} className={`h-11 font-heading text-muted-foreground first:pl-5 ${h === "Clips" ? "text-right" : ""}`}>
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
            </TableRow>
          ))}
          {loaded && !vods.length && (
            <TableRow>
              <TableCell colSpan={6} className="py-16 text-center text-muted-foreground">
                No VODs yet. Run clipit on a VOD, or clipit --poll.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
