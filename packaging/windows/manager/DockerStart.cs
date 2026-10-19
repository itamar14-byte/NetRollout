// NetRollout Manager's Docker Desktop start at sign-in (run by the port helper).

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace NetRollout
{
	// NetRollout comes back by itself once Docker runs (its containers'
	// restart: unless-stopped), but Docker Desktop starts at sign-in only when
	// its own setting says so, and that is often off. The port helper starts
	// at every sign-in and calls Later(): after a pause, so that Docker's own
	// start at sign-in goes first, Docker Desktop is started when it isn't
	// running. Once per helper start: Docker quit later stays quit.
	static class DockerStart
	{
		const int PauseMs = 30000;
		static readonly string Exe = Path.Combine(
			Environment.GetEnvironmentVariable("ProgramW6432") ??
				Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
			@"Docker\Docker\Docker Desktop.exe");

		public static void Later()
		{
			var pause = new Thread(delegate ()
			{
				Thread.Sleep(PauseMs);
				Now();
			});
			pause.IsBackground = true;
			pause.Start();
		}

		// Docker Desktop started when NetRollout is installed here, Docker
		// Desktop is and it isn't running; the outcome in the helper's log
		static void Now()
		{
			try
			{
				if (!Install.Installed || !File.Exists(Exe)) return;
				if (Process.GetProcessesByName("Docker Desktop").Length > 0) return;
				Process.Start(new ProcessStartInfo(Exe) { UseShellExecute = true });
				PortHelper.Log("Docker Desktop wasn't running: started it (NetRollout comes back once it runs)");
			}
			catch (Exception e)
			{
				PortHelper.Log("Docker Desktop couldn't be started: " + e.Message);
			}
		}
	}
}
