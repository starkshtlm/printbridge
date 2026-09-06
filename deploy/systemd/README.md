# The watchdog timer

`printbridge doctor` answers "what is wrong?" when you ask. The watchdog asks for you, every 15
minutes, and sends one message when the answer *changes* — which is what turns "a firmware update
killed the route to the printer" into a notification instead of a mystery three weeks later.

## System-wide (root)

```bash
sudo install -m 644 deploy/systemd/printbridge-doctor.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/printbridge-doctor.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printbridge-doctor.timer
systemctl list-timers printbridge-doctor.timer
```

The unit assumes the repo is at `$HOME/printbridge` of the user running it; edit
`WorkingDirectory=` and `Environment=PRINTBRIDGE_DIR=` if it is somewhere else.

## Without root (user units)

If you cannot become root but your account can run Docker, the same timer works as a user unit —
and with lingering enabled it survives logout and starts at boot:

```bash
mkdir -p ~/.config/systemd/user
sed 's|%h/printbridge|'"$PWD"'|g' deploy/systemd/printbridge-doctor.service \
    > ~/.config/systemd/user/printbridge-doctor.service
cp deploy/systemd/printbridge-doctor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now printbridge-doctor.timer
loginctl enable-linger "$USER"          # so it runs when you are not logged in
```

## Cron instead

```cron
*/15 * * * * cd ~/printbridge && ./deploy/systemd/printbridge-watchdog.sh >/dev/null 2>&1
```

## Turning it off

```bash
systemctl --user disable --now printbridge-doctor.timer     # or: sudo systemctl disable --now …
```

Nothing else in printbridge depends on the timer; alerts simply stop.
