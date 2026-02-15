# code-server Setup Guide for Windows
*Self-hosted VS Code accessible from any browser*

## What This Does
- Runs VS Code in your browser
- Auto-starts when your PC turns on
- Access from any device on your network (phone, laptop, etc.)
- No resource limits - uses your PC's hardware

---

## Part 1: Installation

### Step 1: Download code-server
1. Go to: https://github.com/coder/code-server/releases
2. Download the latest `.zip` file (look for `code-server-X.X.X-windows-amd64.zip`)
3. Extract to a permanent location (e.g., `C:\code-server\`)

### Step 2: Initial Setup
1. Open the extracted folder (`C:\code-server\`)
2. Hold `Shift` + Right-click empty space in folder
3. Select "Open PowerShell window here" (or "Open in Terminal")
4. Run this command:
```powershell
.\code-server.exe --install-extension ms-python.python
```
5. Code-server will start for the first time
6. You'll see output like: `[2024-XX-XX] info  HTTP server listening on http://127.0.0.1:8080/`

### Step 3: Get Your Password
1. The password is auto-generated on first run
2. Find it in: `C:\Users\YourUsername\.config\code-server\config.yaml`
3. Open that file in Notepad
4. Copy the password (you'll need this to log in)

**Example config.yaml:**
```yaml
bind-addr: 127.0.0.1:8080
auth: password
password: abc123xyz456  ← This is your password
cert: false
```

### Step 4: Test It
1. Open browser (Chrome, Edge, Firefox)
2. Go to: `http://localhost:8080`
3. Enter the password from config.yaml
4. You should see VS Code in your browser!

---

## Part 2: Auto-Start on Boot

### Method 1: Task Scheduler (Recommended - Runs in Background)

**Step 1: Create the Task**
1. Press `Win + R`, type `taskschd.msc`, press Enter
2. In Task Scheduler, click "Create Basic Task..." (right side)
3. Name it: `code-server`
4. Description: `Auto-start code-server on login`
5. Click Next

**Step 2: Set Trigger**
1. Select "When I log on"
2. Click Next

**Step 3: Set Action**
1. Select "Start a program"
2. Click Next
3. In "Program/script", browse to: `C:\code-server\code-server.exe`
4. Leave "Add arguments" empty
5. Click Next, then Finish

**Step 4: Configure Advanced Settings**
1. In Task Scheduler, find your "code-server" task (bottom list)
2. Right-click → Properties
3. Under "General" tab:
   - Check "Run whether user is logged on or not"
   - Check "Run with highest privileges"
4. Under "Settings" tab:
   - Uncheck "Stop the task if it runs longer than"
   - Check "If the task is already running, do not start a new instance"
5. Click OK

**Step 5: Test the Task**
1. In Task Scheduler, right-click your "code-server" task
2. Click "Run"
3. Open browser → `http://localhost:8080`
4. Should work!

### Method 2: Startup Folder (Alternative - Shows Window)

**Only use this if Method 1 doesn't work for you**

1. Press `Win + R`, type `shell:startup`, press Enter
2. Right-click in folder → New → Shortcut
3. For location, browse to: `C:\code-server\code-server.exe`
4. Name it: `code-server`
5. Click Finish

**To hide the window:**
1. Right-click the shortcut → Properties
2. Change "Run" to "Minimized"
3. Click OK

---

## Part 3: Access from Other Devices

### Find Your PC's IP Address
1. Open Command Prompt (`Win + R` → `cmd`)
2. Type: `ipconfig`
3. Look for "IPv4 Address" under your active network (usually starts with 192.168)
4. Example: `192.168.1.150`

### Update config.yaml for Network Access
1. **STOP code-server first** (close the window or kill in Task Manager)
2. Open: `C:\Users\YourUsername\.config\code-server\config.yaml`
3. Change this line:
```yaml
bind-addr: 127.0.0.1:8080
```
To this:
```yaml
bind-addr: 0.0.0.0:8080
```
4. Save the file
5. Restart code-server (run the Task Scheduler task or restart PC)

### Allow Through Firewall
1. Press `Win + R`, type `wf.msc`, press Enter
2. Click "Inbound Rules" (left side)
3. Click "New Rule..." (right side)
4. Select "Port" → Next
5. Select "TCP", enter port: `8080` → Next
6. Select "Allow the connection" → Next
7. Check all three: Domain, Private, Public → Next
8. Name it: `code-server` → Finish

### Access from Phone/Other Device
- On same WiFi network, open browser
- Go to: `http://YOUR-PC-IP:8080`
- Example: `http://192.168.1.150:8080`
- Enter your password from config.yaml

---

## Part 4: Changing Your Password (Optional)

1. Stop code-server
2. Open: `C:\Users\YourUsername\.config\code-server\config.yaml`
3. Change the password line to whatever you want:
```yaml
password: myNewPassword123
```
4. Save and restart code-server

---

## Part 5: Useful Commands

### Check if code-server is Running
1. Open Task Manager (`Ctrl + Shift + Esc`)
2. Look for `code-server.exe` in Processes tab

### Manually Stop code-server
1. Task Manager → Find `code-server.exe`
2. Right-click → End Task

### Manually Start code-server
- Method 1 users: Task Scheduler → Right-click task → Run
- Method 2 users: Double-click shortcut in Startup folder
- Or: Open folder `C:\code-server\` → Double-click `code-server.exe`

### View Logs (if something breaks)
1. Code-server logs are in: `C:\Users\YourUsername\.local\share\code-server\`
2. Look at the latest log file

---

## Troubleshooting

### "Can't connect to localhost:8080"
- Check if code-server is running in Task Manager
- Try stopping and starting the task
- Check config.yaml has correct port (8080)

### "Can't access from phone/other device"
- Confirm firewall rule is enabled
- Confirm config.yaml has `bind-addr: 0.0.0.0:8080`
- Make sure devices are on same WiFi
- Double-check your PC's IP address (it might change)

### "Password not working"
- Copy password exactly from config.yaml (no extra spaces)
- Check you're opening the right config file
- Password is case-sensitive

### Code-server not auto-starting
- Check Task Scheduler task is enabled
- Make sure path to code-server.exe is correct
- Try running task manually first to test

### Port 8080 already in use
- Some other app is using that port
- Edit config.yaml and change port to `8081` or `8090`
- Update firewall rule to match new port

---

## Security Notes

**For Local Network Use:**
- Current setup is fine - password protected
- Only accessible on your home WiFi

**For Internet Access (Advanced):**
- DO NOT just port forward 8080 on your router
- Use Tailscale (free VPN) or Cloudflare Tunnel instead
- Ask Claude for help setting this up if needed

---

## Quick Reference Card

**Access URLs:**
- From this PC: `http://localhost:8080`
- From other devices: `http://YOUR-PC-IP:8080`

**Config file location:**
- `C:\Users\YourUsername\.config\code-server\config.yaml`

**Installation folder:**
- `C:\code-server\`

**Password location:**
- Same as config file

**To restart:**
- Task Scheduler → Right-click task → Run
- Or restart your PC

---

## Next Steps

After this is working, you might want to:
1. Install extensions (Python, Prettier, GitLens, etc.)
2. Set up Tailscale for secure remote access
3. Connect to your Amazon FBA project folders
4. Set up Git integration

Need help with any of these? Just ask Claude!
