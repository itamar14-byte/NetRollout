// NetRollout Manager: whether NetRollout's nginx holds its HTTPS port and,
// when it doesn't, who does - asked of manage.ps1 port-check -Json, the same
// lookup `netrollout status` and `netrollout start` use (another container
// by its name, else the program listening there).

using System;
using System.Collections;
using System.Web.Script.Serialization;

namespace NetRollout
{
	static class PortCheck
	{
		// who holds the HTTPS port instead of NetRollout's nginx; "" when
		// nobody does (or nginx holds it); null when it couldn't be asked
		// (Docker not running). Blocks for a few seconds: not on the window's thread.
		public static string Blocker()
		{
			string output;
			if (Rollouts.RunScript("port-check -Json", out output) != 0) return null;
			string line = null;
			foreach (string l in output.Split('\n'))
				if (l.Trim().StartsWith("{")) line = l.Trim();
			if (line == null) return null;
			try
			{
				var data = new JavaScriptSerializer().DeserializeObject(line) as IDictionary;
				if (data == null) return null;
				if (data["published"] is bool && (bool)data["published"]) return "";
				return Convert.ToString(data["owner"]);
			}
			catch (Exception) { return null; }
		}

		// the status line (and the tray's notification) for a taken port
		public static string Words(string port, string owner)
		{
			return "Port " + port + " is taken by " + owner + " - stop it, then Start";
		}
	}
}
