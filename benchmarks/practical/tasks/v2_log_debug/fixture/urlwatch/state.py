class State:
    def __init__(self): self.values={}
    def set(self,key,value): self.values[key]=value
    def get(self,key,default=None): return self.values.get(key,default)
