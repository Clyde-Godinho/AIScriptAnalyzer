import { HEALTH_META, scoreColor } from "@/lib/ui";
import type { HealthLevel } from "@/lib/ui";

export default function HealthBadge({ level, score }: { level: HealthLevel; score?: number | null }) {
  const meta = HEALTH_META[level];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${meta.badge}`}
      >
        <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
        {meta.label}
      </span>
      {score != null && <span className={`font-mono text-xs ${scoreColor(score)}`}>{score}</span>}
    </span>
  );
}
