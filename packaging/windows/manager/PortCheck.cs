// NetRollout Manager: whether NetRollout's nginx holds its HTTPS port and,
// when it doesn't, who does - asked of manage.ps1 port-check -Json, the same
// lookup `netrollout status` and `netrollout start` use (another container
// by its name, else the program listening there).

using System;
using System.Collections;
using System.Drawing;
using System.Web.Script.Serialization;
using System.Windows.Forms;

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
			return "Port " + port + " is taken by " + owner + ". Close it and click Start - " +
				"or click Change port... to use another port for NetRollout.";
		}

		// Change port... greyed (the port is NetRollout's): where it's changed
		public const string Hint = "Change the port in System Settings -> HTTPS port";
	}

	// Change port...: the new port, a number between 1 and 65535 but not 80
	// (manage.ps1 port checks it again, and that it's free)
	class PortDialog : Form
	{
		readonly NumericUpDown number = new NumericUpDown { Minimum = 1, Maximum = 65535, Width = 110 };

		public int Port { get { return (int)number.Value; } }

		public PortDialog(string current)
		{
			Text = "Change the HTTPS port";
			FormBorderStyle = FormBorderStyle.FixedDialog;
			MaximizeBox = MinimizeBox = false;
			ShowInTaskbar = false;
			StartPosition = FormStartPosition.CenterParent;
			Font = new Font("Segoe UI", 9.5f);
			ClientSize = new Size(420, 150);
			var label = new Label
			{
				Text = "Port " + current + " is taken by another program. NetRollout's new HTTPS port:",
				Location = new Point(16, 16), Size = new Size(390, 40)
			};
			int wanted;
			number.Value = int.TryParse(current, out wanted) && wanted < 65535 && wanted != 443 ? wanted + 1 : 8443;
			number.Location = new Point(16, 62);
			var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Location = new Point(224, 106), Width = 88 };
			var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Location = new Point(318, 106), Width = 88 };
			ok.Click += delegate (object s, EventArgs e)
			{
				if (Port == 80 || Port.ToString() == current)
				{
					MessageBox.Show(this, Port == 80 ? "Port 80 is for the http -> https redirect - pick another one."
						: "That's the port that is taken - pick another one.", Text);
					DialogResult = DialogResult.None;
				}
			};
			Controls.AddRange(new Control[] { label, number, ok, cancel });
			AcceptButton = ok;
			CancelButton = cancel;
		}
	}
}
