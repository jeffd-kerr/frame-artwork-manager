/*
 * Launcher for the Frame Artwork Manager app bundle (macOS).
 *
 * This is a template: build_app.sh substitutes the three @PLACEHOLDER@
 * paths below with the real ones on your machine, compiles the result,
 * and installs it as the executable inside FrameArtwork.app.
 *
 * Why a compiled launcher at all? macOS only grants durable
 * local-network access to processes that run as part of an app bundle
 * (see "Running on a Mac" in the README). The bundle needs a real
 * Mach-O executable as its entry point; this one simply starts the
 * Python server with the right working directory and data directory,
 * appending all output to app.log in the data directory.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>

int main(void) {
    const char *appdir  = "@APP_DIR@";
    const char *datadir = "@DATA_DIR@";
    const char *python  = "@PYTHON@";

    char logpath[4096];
    snprintf(logpath, sizeof logpath, "%s/app.log", datadir);
    int fd = open(logpath, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) {
        dup2(fd, 1);
        dup2(fd, 2);
        if (fd > 2) close(fd);
    }
    if (chdir(appdir) != 0) return 1;
    setenv("FRAME_DATA_DIR", datadir, 1);
    execl(python, python, "-u", "frame_app.py", (char *)NULL);
    return 1;
}
