classdef Widget
  methods
    function obj = Widget(name)
      obj.name = name;
    end
    function obj = rename(obj, name)
      obj.name = name;
    end
  end
end
