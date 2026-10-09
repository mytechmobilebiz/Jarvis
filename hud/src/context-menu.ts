import type { ContextMenuParams, MenuItemConstructorOptions } from "electron";

/**
 * Electron ships no context menu, so a right-click on the wizard's API-key
 * field used to do nothing at all — the only paste affordance was a Ctrl/Cmd+V
 * chord. Kept as a pure function so it can be unit-tested without booting
 * Electron; main.ts feeds the result to Menu.buildFromTemplate.
 */
export function buildContextMenuTemplate(params: ContextMenuParams): MenuItemConstructorOptions[] {
  const flags = params.editFlags;
  const hasSelection = params.selectionText.trim() !== "";

  if (params.isEditable) {
    return [
      { role: "undo", enabled: flags.canUndo },
      { role: "redo", enabled: flags.canRedo },
      { type: "separator" },
      { role: "cut", enabled: flags.canCut && hasSelection },
      { role: "copy", enabled: flags.canCopy && hasSelection },
      { role: "paste", enabled: flags.canPaste },
      { type: "separator" },
      { role: "selectAll", enabled: flags.canSelectAll },
    ];
  }

  if (hasSelection) {
    return [
      { role: "copy", enabled: flags.canCopy },
      { type: "separator" },
      { role: "selectAll", enabled: flags.canSelectAll },
    ];
  }

  return [];
}
