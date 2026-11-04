import { HeadContent, Scripts, createRootRoute } from '@tanstack/react-router'
import { REPO, SITE_URL, SUMMARY } from '#/content'
import appCss from '../styles.css?url'
// The same files styles.css asks for, preloaded so they don't wait for the CSS to arrive first.
import bricolage from '@fontsource-variable/bricolage-grotesque/files/bricolage-grotesque-latin-standard-normal.woff2?url'
import mono from '@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2?url'

const TITLE = 'Clipit: turn Twitch and Kick streams into Shorts and TikToks'

const LD = JSON.stringify({
  '@context': 'https://schema.org',
  '@type': 'SoftwareApplication',
  name: 'Clipit',
  applicationCategory: 'MultimediaApplication',
  operatingSystem: 'Windows, macOS, Linux',
  description: SUMMARY,
  license: 'https://opensource.org/licenses/MIT',
  codeRepository: REPO,
  offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
})

const absolute = SITE_URL
  ? [
      { property: 'og:url', content: `${SITE_URL}/` },
      { property: 'og:image', content: `${SITE_URL}/og.png` },
      { property: 'og:image:width', content: '1200' },
      { property: 'og:image:height', content: '630' },
      { property: 'og:image:alt', content: 'Clipit: a four-hour stream timeline with three picked moments turned into vertical clips' },
      { name: 'twitter:card', content: 'summary_large_image' },
    ]
  : []

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: 'utf-8' },
      { name: 'viewport', content: 'width=device-width, initial-scale=1' },
      { title: TITLE },
      { name: 'description', content: SUMMARY },
      { property: 'og:type', content: 'website' },
      { property: 'og:site_name', content: 'Clipit' },
      { property: 'og:title', content: TITLE },
      { property: 'og:description', content: SUMMARY },
      ...absolute,
      { name: 'theme-color', content: '#160b2b' },
    ],
    links: [
      { rel: 'preload', href: bricolage, as: 'font', type: 'font/woff2', crossOrigin: 'anonymous' },
      { rel: 'preload', href: mono, as: 'font', type: 'font/woff2', crossOrigin: 'anonymous' },
      { rel: 'stylesheet', href: appCss },
      { rel: 'icon', href: '/favicon.svg', type: 'image/svg+xml' },
      ...(SITE_URL ? [{ rel: 'canonical', href: `${SITE_URL}/` }] : []),
    ],
  }),
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <HeadContent />
        <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: LD }} />
      </head>
      <body>
        {children}
        <Scripts />
      </body>
    </html>
  )
}
