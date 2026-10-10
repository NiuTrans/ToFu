/**
 * tofu-ui — the design-system adapter layer.
 *
 * Application code imports factories ONLY from this module; it never
 * hand-writes ui-* class strings and never reaches into kit internals.
 * Styles live in frontend/src/styles/application/30-ui-kit.css, tokens in
 * 00-design-tokens.css; the contract is documented in docs/DESIGN_SYSTEM.md.
 */

export { createButton, createIconButton } from './button';
export type { ButtonOptions, ButtonSize, ButtonVariant, IconButtonOptions } from './button';
export { createField, setFieldError } from './field';
export type { FieldHandle, FieldOptions } from './field';
export { createDialog } from './dialog';
export type { DialogAction, DialogHandle, DialogOptions } from './dialog';
export { createDrawer } from './drawer';
export type { DrawerHandle, DrawerOptions } from './drawer';
export { createChoice } from './choice';
export type { ChoiceHandle, ChoiceOptions, ChoiceType } from './choice';
export { createTabs } from './tabs';
export type { TabItem, TabsHandle, TabsOptions } from './tabs';
export { createDropdown, createMenuItem } from './dropdown';
export type { DropdownHandle, DropdownOptions, MenuEntry, MenuItem } from './dropdown';
export { dismissAllToasts, toast } from './toast';
export type { ToastHandle, ToastOptions, ToastVariant } from './toast';
export { createAlert } from './alert';
export type { AlertHandle, AlertOptions, AlertVariant } from './alert';
export { createEmptyState, createProgress, createSkeleton, createSpinner } from './feedback';
export type {
  EmptyStateOptions,
  ProgressHandle,
  ProgressOptions,
  SkeletonOptions,
  SpinnerOptions,
} from './feedback';
