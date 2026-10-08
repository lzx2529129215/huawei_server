import { UIAbility, Want, AbilityConstant } from '@kit.AbilityKit';
import { window, promptAction } from '@kit.ArkUI';
import memload from 'libmemload.so';

export default class EntryAbility extends UIAbility {
  private caseId: string = '';
  private warned: boolean = false;
  private foreground: boolean = false;
  private pendingLevel: number = -1;
  private bundleName: string = 'unknown';

  private configure(want: Want): void {
    const p = want.parameters;
    if (!p) { return; }
    const op = String(p['op'] ?? 'start');
    if (op === 'promote') {
      const promoteCase = String(p['case_id'] ?? '');
      if (promoteCase === this.caseId) { memload.promote(promoteCase); memload.record('promote_requested', 0); }
      return;
    }
    if (op === 'focus') { return; }
    if (op === 'stop') { memload.stop(); return; }
    if (op === 'retouch') { memload.retouch(); return; }
    const id = String(p['case_id'] ?? 'manual');
    if (id === this.caseId) { return; }
    this.caseId = id;
    this.bundleName = String(p['bundle'] ?? 'unknown');
    this.warned = false;
    this.pendingLevel = -1;
    AppStorage.setOrCreate<string>('caseId', id);
    memload.start(Number(p['daily_mib'] ?? 64), Number(p['peak_mib'] ?? 128),
      Number(p['file_mib'] ?? 0), Number(p['hold_seconds'] ?? 5),
      Number(p['lifetime_seconds'] ?? 180), id, String(p['daily_case_id'] ?? `${id}_daily`),
      this.context.filesDir, Number(p['seed'] ?? 1), Number(p['await_peak'] ?? 0) === 1);
  }

  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    this.configure(want);
  }
  onNewWant(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    this.configure(want);
  }
  onWindowStageCreate(stage: window.WindowStage): void {
    stage.loadContent('pages/Index');
  }
  onForeground(): void {
    this.foreground = true;
    if (this.pendingLevel >= 0) { this.showWarning(this.pendingLevel); }
  }
  onBackground(): void { this.foreground = false; }
  onDestroy(): void { memload.stop(); }
  onMemoryLevel(level: AbilityConstant.MemoryLevel): void {
    memload.record('memory_notification', Number(level));
    if (level === AbilityConstant.MemoryLevel.MEMORY_LEVEL_LOW ||
      level === AbilityConstant.MemoryLevel.MEMORY_LEVEL_CRITICAL ||
      level === AbilityConstant.MemoryLevel.MEMORY_LEVEL_BACKGROUND_LOW ||
      level === AbilityConstant.MemoryLevel.MEMORY_LEVEL_BACKGROUND_CRITICAL) {
      this.pendingLevel = Number(level);
      if (this.foreground) { this.showWarning(Number(level)); }
    }
  }
  private showWarning(level: number): void {
    if (this.warned) { return; }
    this.warned = true;
    memload.record('popup_requested', level);
    promptAction.showDialog({
      title: '内存不足',
      message: `系统已发出低内存通知。用例 ${this.caseId}，应用 ${this.bundleName}。本次压力周期仅提示一次。`,
      buttons: [{ text: '确认', color: '#246BCE' }]
    }).catch(() => { memload.record('popup_display_error', level); });
  }
}
