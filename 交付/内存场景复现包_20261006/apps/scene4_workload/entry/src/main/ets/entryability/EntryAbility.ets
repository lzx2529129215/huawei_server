import { UIAbility, Want, AbilityConstant } from '@kit.AbilityKit';
import { window, promptAction } from '@kit.ArkUI';
import memload from 'libmemload.so';

export default class EntryAbility extends UIAbility {
  private caseId: string = '';
  private warned: boolean = false;
  private foreground: boolean = false;
  private pendingLevel: number = -1;
  private bundleName: string = 'unknown';
  private pageContentReady: boolean = false;
  private pendingLaunch: Want | undefined = undefined;
  private donorRole: boolean = false;

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
    if (op === 'donor_validate') { memload.validateDonor(); return; }
    const id = String(p['case_id'] ?? 'manual');
    if (id === this.caseId) { return; }
    if (!this.pageContentReady) { this.pendingLaunch = want; return; }
    this.caseId = id;
    this.bundleName = String(p['bundle'] ?? 'unknown');
    this.donorRole = Number(p['donor_mode'] ?? 0) === 1;
    const coldReady = Number(p['cold_start_ready'] ?? 1) === 1;
    this.warned = false;
    this.pendingLevel = -1;
    AppStorage.setOrCreate<string>('caseId', id);
    AppStorage.setOrCreate<boolean>('coldStartReadyEnabled', coldReady);
    memload.start(Number(p['daily_mib'] ?? 64), Number(p['peak_mib'] ?? 128),
      Number(p['file_mib'] ?? 0), Number(p['hold_seconds'] ?? 5),
      Number(p['lifetime_seconds'] ?? 180), id, String(p['daily_case_id'] ?? `${id}_daily`),
      this.context.filesDir, Number(p['seed'] ?? 1), Number(p['await_peak'] ?? 0) === 1,
      coldReady, this.donorRole);
    if (coldReady) { memload.acknowledgeWindowLoaded(); }
  }

  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    this.configure(want);
  }
  onNewWant(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    this.configure(want);
  }
  onWindowStageCreate(stage: window.WindowStage): void {
    stage.loadContent('pages/Index', (err) => {
      const hasError = err !== null && err !== undefined;
      const callbackCode = hasError && typeof err === 'object'
        ? (err as BusinessError).code : undefined;
      console.info(`[MemScene] loadContent callback err=${JSON.stringify(err)} code=${String(callbackCode)}`);
      if (hasError && (typeof callbackCode !== 'number' || callbackCode !== 0)) {
        console.error(`[MemScene] loadContent failed; pending launch will not start, code=${String(callbackCode)}`);
        return;
      }
      this.pageContentReady = true;
      if (this.pendingLaunch) {
        const launch = this.pendingLaunch;
        this.pendingLaunch = undefined;
        this.configure(launch);
      }
    });
  }
  onForeground(): void {
    this.foreground = true;
    memload.setBackground(false);
    if (this.pendingLevel >= 0) { this.showWarning(this.pendingLevel); }
  }
  onBackground(): void {
    this.foreground = false;
    memload.setBackground(true);
  }
  onDestroy(): void {
    if (this.donorRole) { memload.record('donor_ability_destroy', 1); }
    memload.stop();
  }
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
