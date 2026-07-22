import type { ProjectState } from "@/data/roadmap";
import { cn } from "@/lib/utils";

const styles: Record<ProjectState, string> = {
  active: "text-accent",
  paused: "text-paused",
  blocked: "text-err",
  done: "text-ok",
  "not-started": "text-fg-muted",
};

export function StateBadge({ state }: { state: ProjectState }) {
  return (
    <span className={cn("inline-flex items-baseline gap-1.5 text-[0.75rem]", styles[state])}>
      <span aria-hidden>●</span>
      {state}
    </span>
  );
}
