/** Article-only value contracts. Dependencies: DOM and explicit presentation ports. */
export interface GlossaryEntry { term: string; def: string; defHtml: string; analogy: string }
export interface HeadingEntry { id: string; text: string; level: 2 | 3 }
export interface ArticlePorts { openImage(source: string): void; reducedMotion(): boolean }
