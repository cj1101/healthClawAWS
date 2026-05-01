@echo off
setlocal EnableExtensions
rem One-shot: SSH in, git pull ~/healthClaw, restart Nemoclaw API (systemd).
rem Override any variable before calling, e.g. set EC2_USER=ubuntu && ec2-sync-and-restart.cmd

if not defined EC2_KEY set "EC2_KEY=C:\Users\charl\openclawKey.pem"
if not defined EC2_HOST set "EC2_HOST=54.80.131.225"
if not defined EC2_USER set "EC2_USER=ec2-user"
if not defined EC2_REPO set "EC2_REPO=~/healthClaw"

if not exist "%EC2_KEY%" (
  echo ERROR: SSH key not found: "%EC2_KEY%"
  exit /b 1
)

ssh -i "%EC2_KEY%" -o StrictHostKeyChecking=accept-new "%EC2_USER%@%EC2_HOST%" "cd %EC2_REPO% && git pull && sudo systemctl restart nemoclaw-health"
exit /b %ERRORLEVEL%
