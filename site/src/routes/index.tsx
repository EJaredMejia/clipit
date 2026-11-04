import { createFileRoute } from '@tanstack/react-router'
import { AIS, CLIPS, DESCRIPTION, FEATURES, INSTALL, PICKS, REPO, SPECS, STEPS, VOD } from '#/content'
import { ClipFrame } from '#/components/ClipFrame'
import { Timeline } from '#/components/Timeline'
import { CopyButton } from '#/components/CopyButton'

export const Route = createFileRoute('/')({ component: Home })

function Home() {
  return (
    <div className="va">
      <a className="va-skip" href="#main">Skip to content</a>
      <header className="va-bar">
        <a href="/" className="va-logo" aria-label="Clipit home">clipit</a>
        <nav aria-label="Main">
          <a href="#how">How it works</a>
          <a href="#features">Features</a>
          <a href="#install">Install</a>
          <a href={REPO} className="va-gh">GitHub</a>
        </nav>
      </header>

      <main id="main">
        <section className="va-hero va-wrap">
          <h1>
            <span>Four hours of stream.</span> <span>The forty seconds worth posting.</span>
          </h1>
          <div className="va-hero-copy">
            <p className="va-lede">{DESCRIPTION}</p>
            <div className="va-ctas">
              <a className="va-btn" href="#install">Install Clipit</a>
              <a className="va-btn va-btn-quiet" href={REPO}>View on GitHub</a>
            </div>
          </div>
          <dl className="va-specs">
            {SPECS.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="va-stage va-wrap" aria-label="From VOD to clips">
          <div className="va-stage-head">
            <p>A four-hour VOD, minute by minute</p>
            <ul className="va-legend">
              <li className="loud">Loudness</li>
              <li className="chat">Chat messages</li>
              <li className="pick">Picked clip</li>
            </ul>
          </div>
          <Timeline />
          <div className="va-clips">
            {CLIPS.map((c, i) => (
              <div key={c.hook} className="va-clip" style={{ '--x': `${((PICKS[i] + 2) / VOD.length) * 100}%`, '--i': i } as React.CSSProperties}>
                <ClipFrame clip={c} />
                <span className="va-score">Score {c.score} / 10</span>
              </div>
            ))}
          </div>
        </section>

        <section id="how" className="va-section va-wrap">
          <header className="va-section-head">
            <h2>How it works</h2>
            <p>One pass over a stream, start to finish. Each step caches its work, so a crash picks up where it stopped.</p>
          </header>
          <ol className="va-steps">
            {STEPS.map((s, i) => (
              <li key={s.title}>
                <span className="va-step-n">{i + 1}</span>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <section id="features" className="va-section va-wrap">
          <header className="va-section-head">
            <h2>Features</h2>
            <p>Clipit runs on your computer. There is no account to make and no server of ours in between.</p>
          </header>
          <dl className="va-features">
            {FEATURES.map((f) => (
              <div key={f.title}>
                <dt>{f.title}</dt>
                <dd>{f.body}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section id="install" className="va-section va-wrap">
          <header className="va-section-head">
            <h2>Install</h2>
            <p>You need Python 3.11 or newer, ffmpeg, and one AI CLI: {AIS.slice(0, -1).join(', ')}, or any other.</p>
          </header>
          <div className="va-code">
            <pre><code>{INSTALL.join('\n')}</code></pre>
            <CopyButton text={INSTALL.join('\n')} />
            <p className="va-code-note">
              The last line clips one Twitch VOD. Run <code>clipit --poll</code> to clip whoever is trending, or{' '}
              <code>clipit --dashboard</code> to review clips in your browser.
            </p>
          </div>
        </section>
      </main>

      <footer className="va-foot va-wrap">
        <div>
          <span className="va-logo">clipit</span>
          <p>Free and open source under the MIT license.</p>
        </div>
        <nav aria-label="Project">
          <a href={REPO}>Source code</a>
          <a href={`${REPO}#readme`}>Documentation</a>
          <a href={`${REPO}/issues`}>Report an issue</a>
          <a href={`${REPO}/blob/main/LICENSE`}>License</a>
        </nav>
      </footer>
    </div>
  )
}
