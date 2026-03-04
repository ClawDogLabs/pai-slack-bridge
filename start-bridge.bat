@echo off
set HOME=C:\Users\steven.wu
set PAI_DIR=C:\Users\steven.wu\.claude
set PATH=%PATH%;C:\Users\steven.wu\.bun\bin;C:\Users\steven.wu\AppData\Roaming\npm
cd /d C:\Users\steven.wu\Documents\ClaudeCodeSlackBridge\pai-slack-bridge
C:\Users\steven.wu\.bun\bin\bun.exe run src/index.ts
