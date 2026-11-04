import { createFileRoute } from '@tanstack/react-router'
import { CLIPS } from '#/content'
import { ClipFrame } from '#/components/ClipFrame'
import { Timeline } from '#/components/Timeline'

// The 1200×630 share image, screenshotted into public/og.png (see README). Not for visitors.
export const Route = createFileRoute('/og')({
  head: () => ({ meta: [{ name: 'robots', content: 'noindex' }] }),
  component: Og,
})

function Og() {
  return (
    <div className="va va-og">
      <div className="va-og-top">
        <div>
          <span className="va-logo">clipit</span>
          <h1>
            <span>Four hours of stream.</span> <span>The forty seconds worth posting.</span>
          </h1>
          <p>Free, open-source clipping for Twitch and Kick.</p>
        </div>
        <div className="va-og-clips">
          {CLIPS.map((c) => <ClipFrame key={c.hook} clip={c} />)}
        </div>
      </div>
      <Timeline />
    </div>
  )
}
