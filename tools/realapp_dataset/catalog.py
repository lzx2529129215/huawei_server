"""Intent catalog. A scenario is not executable until UI recipes are calibrated."""
APPS = {
    "抖音": "com.ss.ohpc.ugc.aweme", "悟空图像": "com.photosir.himirage",
    "备忘录": "com.huawei.hmos.notepad", "华为视频": "com.huawei.hmsapp.himovie",
    "WPS": "cn.wps.office.hap", "海泰浏览器": "com.haitai.htbrowser",
    "coremail": "cn.coremail.chordmail", "小红书": "com.xingin.xhs_hos", "飞书": "com.ss.feishu_pc",
    "华为浏览器": "com.huawei.hmos.browser", "华为邮箱": "com.huawei.hmos.email",
    "微信": "com.tencent.wechat.pc", "mindmaster": "com.edrawsoft.mindmaster.pc", "中望CAD": "com.zwsoft.zwcad.PE",
    "钉钉": "com.dingtalk.hmos.pc", "哔哩哔哩": "yylx.danmaku.bili",
    "亿图图示": "com.edrawsoft.edrawmax.pc", "剪映": "com.lemon.lvpro",
    "日历": "com.huawei.hmos.calendar", "华为音乐": "com.huawei.hmsapp.music",
}
SEQUENCES = [
    ("office", "办公协作", [("日历", "查看测试日程"), ("飞书", "查看测试文档"), ("WPS", "打开测试文档和表格并重算"), ("coremail", "预览测试附件"), ("华为邮箱", "预览测试附件"), ("微信", "查看测试文件"), ("WPS", "重访并编辑副本")]),
    ("media", "视频社交", [("华为音乐", "播放测试音频"), ("抖音", "切换固定视频列表"), ("小红书", "浏览固定图文"), ("哔哩哔哩", "播放并拖动进度"), ("华为视频", "播放测试视频"), ("抖音", "重访视频"), ("哔哩哔哩", "重访视频")]),
    ("editing", "视频制作", [("华为浏览器", "查看测试素材页"), ("悟空图像", "打开多张大图并编辑"), ("剪映", "导入预览并导出测试视频"), ("备忘录", "编辑测试笔记"), ("剪映", "重访并预览")]),
    ("design", "CAD与方案", [("钉钉", "查看测试项目资料"), ("中望CAD", "打开图纸缩放并切换图层"), ("亿图图示", "编辑测试图示"), ("mindmaster", "展开测试导图"), ("WPS", "编辑测试报告"), ("中望CAD", "重访图纸")]),
    ("research", "双浏览器资料整理", [("海泰浏览器", "打开固定资料页"), ("华为浏览器", "打开另一组固定资料页"), ("coremail", "预览测试附件"), ("飞书", "查看测试文档"), ("WPS", "打开资料副本"), ("海泰浏览器", "重访资料页"), ("华为浏览器", "重访资料页")]),
    ("mixed", "混合高峰", [("华为音乐", "后台播放测试音频"), ("WPS", "打开测试资料"), ("中望CAD", "打开测试图纸"), ("悟空图像", "编辑测试大图"), ("剪映", "预览并导出测试视频"), ("华为视频", "播放测试视频"), ("微信", "查看测试文件"), ("钉钉", "查看测试项目资料"), ("剪映", "重访预览"), ("中望CAD", "重访图纸")]),
]


def suite():
    return {"schema": "realapp-dd-dataset-v1", "reference_gib": 8, "tmpfs_limit_gib": 32,
            "baseline_seconds": 60, "action_seconds": 1200, "post_seconds": 60,
            "watchdog_seconds": 1800, "sampling_ms": 250,
            "apps": {name: {"bundle": bundle, "form": "native" if bundle else None,
                             "ability": None, "module": None, "verified": False}
                     for name, bundle in APPS.items()},
            "scenes": [{"id": sid, "name": name, "steps": [
                {"id": f"{sid}_{i+1:02d}", "app": app, "intent": intent,
                 "recipe": {"setup": [], "actions": [], "postcondition": {}, "materials": [],
                            "timeout_seconds": 120, "dwell_seconds": 15}}
                for i, (app, intent) in enumerate(steps)]} for sid, name, steps in SEQUENCES]}
