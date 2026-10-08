from .processpipe import Process


class LocalFileProcess(Process):
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def name(self):
        return "localfile"

    def start(self, args):
        self.args = {}
        self.args["outfile"] = self.filepath
        self.msg_ready(self.args)

    def stop(self):
        self.msg_finished()
