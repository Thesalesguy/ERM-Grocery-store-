interface PlaceholderPageProps {
  title: string
  description: string
  milestone: string
}

/**
 * Shared shell for a module page that hasn't been implemented yet. Once a
 * module is built (see docs/TECHNICAL_BLUEPRINT.md Section M), its page
 * file stops rendering this and renders real UI instead.
 */
export function PlaceholderPage({ title, description, milestone }: PlaceholderPageProps) {
  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold text-gray-900">{title}</h1>
      <p className="mt-2 text-gray-600">{description}</p>
      <p className="mt-4 inline-block rounded bg-amber-100 px-3 py-1 text-sm text-amber-800">
        Planned for {milestone} — see docs/TECHNICAL_BLUEPRINT.md
      </p>
    </div>
  )
}
