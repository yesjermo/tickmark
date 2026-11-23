/** The Tickmark mark: a tick drawn as a rising right triangle, the same shape as the favicon. */
export function Mark({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden className="mark">
      <path d="M15 1v14H1z" fill="currentColor" />
    </svg>
  );
}
