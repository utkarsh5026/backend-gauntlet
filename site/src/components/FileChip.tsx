import type { IconType } from 'react-icons'
import {
  SiRust,
  SiDocker,
  SiTypescript,
  SiToml,
  SiJson,
  SiMarkdown,
  SiYaml,
  SiGnubash,
} from 'react-icons/si'
import { VscFile, VscDatabase } from 'react-icons/vsc'

/**
 * A depicted source file, shown as a language-tagged chip that links to the
 * file on GitHub. Icons are real brand marks from react-icons, tinted per
 * language so a file is recognizable at a glance; the label is the path
 * relative to the project.
 */

type Lang = { color: string; Icon: IconType }

/** Map a file path to its language color + icon. */
function langOf(path: string): Lang {
  const base = path.split('/').pop() ?? path
  const ext = base.includes('.') ? base.split('.').pop()!.toLowerCase() : ''
  const isCompose = /^(docker-)?compose\.ya?ml$/.test(base)

  if (base === 'Dockerfile' || isCompose) return { color: '#4a9fd8', Icon: SiDocker }
  switch (ext) {
    case 'rs':
      return { color: '#d98a5b', Icon: SiRust }
    case 'sql':
      return { color: '#6b9fd8', Icon: VscDatabase }
    case 'toml':
    case 'lock':
      return { color: '#b58a5e', Icon: SiToml }
    case 'ts':
    case 'tsx':
      return { color: '#5b9bd5', Icon: SiTypescript }
    case 'json':
      return { color: '#ceac63', Icon: SiJson }
    case 'sh':
      return { color: '#79b878', Icon: SiGnubash }
    case 'md':
      return { color: '#8a94a0', Icon: SiMarkdown }
    case 'yml':
    case 'yaml':
      return { color: '#b5896f', Icon: SiYaml }
    default:
      return { color: '#7f8d84', Icon: VscFile }
  }
}

export function FileChip({ path, href }: { path: string; href: string }) {
  const { color, Icon } = langOf(path)
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={`View ${path} on GitHub`}
      className="group/chip inline-flex items-center gap-1.5 text-[0.75rem] text-fg-muted no-underline transition-colors hover:text-fg"
    >
      <Icon
        size={13}
        style={{ color }}
        className="shrink-0 opacity-70 transition-opacity group-hover/chip:opacity-100"
        aria-hidden
      />
      <span>{path}</span>
    </a>
  )
}
