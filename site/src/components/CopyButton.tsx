import { useEffect, useRef, useState } from 'react'

export function CopyButton({ text }: { text: string }) {
  const [label, setLabel] = useState('Copy')
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])

  const show = (next: string) => {
    setLabel(next)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setLabel('Copy'), 1600)
  }

  return (
    <button
      type="button"
      className="va-copy"
      onClick={() => navigator.clipboard.writeText(text).then(() => show('Copied'), () => show('Couldn’t copy'))}
    >
      <span aria-live="polite">{label}</span>
    </button>
  )
}
