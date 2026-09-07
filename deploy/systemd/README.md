# The watchdog timer

`printbridge doctor` answers "what is wrong?" when you ask. The watchdog asks for you, every 15
minutes, and sends one message when the answer *changes* — which is what turns "a firmware update
killed the route to the printer" into a notification instead of a mystery three weeks later. It
also runs the weekly digest, deletes finished spool files and applies `JOB_LOG_RETENTION_DAYS`.

## Install

The unit ships with two placeholders, because the two things it cannot guess are **which account**
owns the checkout and **where** the checkout is. Fill both in:

```bash
sed -e "s|__PRINTBRIDGE_USER__|$USER|g" -e "s|__PRINTBRIDGE_DIR__|$PWD|g" \
    deploy/systemd/printbridge-doctor.service > /tmp/printbridge-doctor.service
sudo install -m 644 /tmp/printbridge-doctor.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/printbridge-doctor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printbridge-doctor.timer
```

Then check it actually ran, rather than assuming:

```bash
sudo systemctl start printbridge-doctor.service
systemctl status printbridge-doctor.service      # must say the last run succeeded
systemctl list-timers printbridge-doctor.timer
```

> **`User=` is not optional.** A system unit without it runs as root, `%h` becomes `/root`, and the
> service fails with `status=203/EXEC` on every tick — a red timer nobody is watching. `printbridge
> doctor` reports a failing watchdog for exactly this reason.

The account needs to be able to run `docker`. If you added it to the `docker` group in this login
session, that membership does not reach systemd until the next reboot.

## Without root (user units)

If your account can run Docker but cannot become root, the same timer works as a user unit, and
with lingering enabled it survives logout and starts at boot:

```bash
mkdir -p ~/.config/systemd/user
sed -e "s|__PRINTBRIDGE_USER__||g" -e "s|__PRINTBRIDGE_DIR__|$PWD|g" \
    deploy/systemd/printbridge-doctor.service | grep -v '^User=$' \
    > ~/.config/systemd/user/printbridge-doctor.service
cp deploy/systemd/printbridge-doctor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now printbridge-doctor.timer
loginctl enable-linger "$USER"
```

**Pick one or the other.** Two copies of the timer means two checks every 15 minutes and duplicate
alerts.

## Cron instead

```cron
*/15 * * * * cd ~/printbridge && ./deploy/systemd/printbridge-watchdog.sh >/dev/null 2>&1
```

## Turning it off

```bash
sudo systemctl disable --now printbridge-doctor.timer     # or: systemctl --user disable --now …
```

Nothing else depends on the timer; alerts simply stop, and `printbridge doctor` starts saying so.
