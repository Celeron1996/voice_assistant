#!/bin/sh
#
# 板端 PulseAudio 修复：禁用 module-suspend-on-idle
#
# 背景：本板 PulseAudio 的 sink 进入 SUSPENDED 后经常无法被新播放流唤醒
# （表现为 paplay 挂起、写超时、无声），导致播放永久卡死。
# 该脚本注释掉 suspend-on-idle 模块加载，让 sink 空闲时保持 IDLE，
# 有播放流时正常变为 RUNNING。
#
# 注意：必须先启动播放保活流（paplay /dev/zero）再启动 arecord，
#       因为 WM8960 在有采集流时打不开播放流。
#
# 用法：scp scripts/fix_pulse_suspend.sh root@<board>:/root/ && sh fix_pulse_suspend.sh

set -e

for f in /etc/pulse/system.pa /etc/pulse/default.pa; do
    [ -f "$f" ] || continue
    [ -f "$f.bak" ] || cp "$f" "$f.bak"
    sed -i 's|^load-module module-suspend-on-idle|# disabled by voice_assistant: suspend-on-idle breaks resume on this board|' "$f"
    echo "已处理 $f"
done

echo "完成。请重启 PulseAudio 生效："
echo "  /etc/init.d/S50pulseaudio stop"
echo "  for p in /proc/[0-9]*; do [ \"\$(cat \$p/comm 2>/dev/null)\" = pulseaudio ] && kill \${p#/proc/}; done"
echo "  /etc/init.d/S50pulseaudio start"
echo "验证：pactl list sinks | grep State   # 空载应为 IDLE，播放时应为 RUNNING"
