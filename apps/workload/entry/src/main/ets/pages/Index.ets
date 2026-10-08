import memload from 'libmemload.so';

@Entry
@Component
struct Index {
  @State detail: string = '等待负载初始化';
  @StorageLink('caseId') caseId: string = '';
  private timer: number = -1;
  aboutToAppear(): void {
    this.timer = setInterval(() => { this.detail = memload.status(); }, 500);
  }
  aboutToDisappear(): void {
    if (this.timer >= 0) { clearInterval(this.timer); }
  }
  build() {
    Column({ space: 24 }) {
      Text('内存场景负载').fontSize(30).fontWeight(FontWeight.Bold)
      Text(this.caseId).fontSize(18)
      Text(this.detail).fontSize(16).id('memscene-status')
      Button('释放本应用负载').onClick(() => { memload.stop(); })
    }.width('100%').height('100%').padding(40).justifyContent(FlexAlign.Center)
    .backgroundColor('#F4F6FA')
  }
}
